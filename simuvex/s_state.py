#!/usr/bin/env python

import functools
import itertools
#import weakref

import logging
l = logging.getLogger("simuvex.s_state")

import ana

def arch_overrideable(f):
    @functools.wraps(f)
    def wrapped_f(self, *args, **kwargs):
        if hasattr(self.arch, f.__name__):
            arch_f = getattr(self.arch, f.__name__)
            return arch_f(self, *args, **kwargs)
        else:
            return f(self, *args, **kwargs)
    return wrapped_f

from .plugins import default_plugins

# This is a counter for the state-merging symbolic variables
merge_counter = itertools.count()

class SimState(ana.Storable): # pylint: disable=R0904
    '''The SimState represents the state of a program, including its memory, registers, and so forth.'''

    def __init__(self, arch="AMD64", plugins=None, memory_backer=None, mode=None, options=None, add_options=None, remove_options=None):
        # the architecture is used for function simulations (autorets) and the bitness
        self.arch = Architectures[arch]() if isinstance(arch, str) else arch
        self.abiv = None

        # the options
        if options is None:
            if mode is None:
                l.warning("SimState defaulting to symbolic mode.")
                mode = "symbolic"
            options = o.default_options[mode]

        options = set(options)
        if add_options is not None:
            options |= add_options
        if remove_options is not None:
            options -= remove_options
        self.options = options
        self.mode = mode

        # plugins
        self.plugins = { }
        if plugins is not None:
            for n,p in plugins.iteritems():
                self.register_plugin(n, p)

        if not self.has_plugin('memory'):
            if o.ABSTRACT_MEMORY in self.options:
                # We use SimAbstractMemory in static mode
                # Convert memory_backer into 'global' region
                if memory_backer is not None:
                    memory_backer = {'global': memory_backer}

                self.register_plugin('memory', SimAbstractMemory(memory_backer, memory_id="mem"))
            else:
                self.register_plugin('memory', SimSymbolicMemory(memory_backer, memory_id="mem"))
        if not self.has_plugin('registers'):
            self.register_plugin('registers', SimSymbolicMemory(memory_id="reg"))
            self.register_plugin('regs', SimRegNameView())

        # This is used in static mode as we don't have any constraints there
        self._satisfiable = True

        # states are big, so let's give them UUIDs for ANA right away to avoid
        # extra pickling
        self.make_uuid()

        # addresses and stuff of what we're currently processing
        self.bbl_addr = None
        self.stmt_idx = None
        self.ins_addr = None
        self.sim_procedure = None

        self.uninitialized_access_handler = None

    def _ana_getstate(self):
        s = dict(ana.Storable._ana_getstate(self))
        s['plugins'] = { k:v for k,v in s['plugins'].iteritems() if k != 'inspector' }
        return s

    def _ana_setstate(self, s):
        ana.Storable._ana_setstate(self, s)
        for p in self.plugins.values():
            p.set_state(self)

    # easier access to some properties

    @property
    def ip(self):
        '''
        Get the instruction pointer expression.
        :return: an expression
        '''
        return self.regs.ip

    @ip.setter
    def ip(self, val):
        self.regs.ip = val

    # accessors for memory and registers and such
    @property
    def memory(self):
        return self.get_plugin('memory')

    @property
    def registers(self):
        return self.get_plugin('registers')

    @property
    def se(self):
        return self.get_plugin('solver_engine')

    @property
    def inspect(self):
        return self.get_plugin('inspector')

    @property
    def log(self):
        return self.get_plugin('log')

    @property
    def posix(self):
        return self.get_plugin('posix')

    @property
    def libc(self):
        return self.get_plugin('libc')

    @property
    def cgc(self):
        return self.get_plugin('cgc')

    @property
    def regs(self):
        return self.get_plugin('regs')

    def _inspect(self, *args, **kwargs):
        if self.has_plugin('inspector'):
            self.inspect.action(*args, **kwargs)

    #
    # Plugins
    #

    def has_plugin(self, name):
        return name in self.plugins

    def get_plugin(self, name):
        if name not in self.plugins:
            p = default_plugins[name]()
            self.register_plugin(name, p)
            return p
        return self.plugins[name]

    def register_plugin(self, name, plugin):
        #l.debug("Adding plugin %s of type %s", name, plugin.__class__.__name__)
        plugin.set_state(self)
        self.plugins[name] = plugin
        return plugin

    def release_plugin(self, name):
        if name in self.plugins:
            del self.plugins[name]

    #
    # Constraint pass-throughs
    #

    def simplify(self, *args): return self.se.simplify(*args)

    def add_constraints(self, *args):
        if len(args) > 0 and type(args[0]) in (list, tuple):
            raise Exception("Tuple or list passed to add_constraints!")

        if o.TRACK_CONSTRAINTS in self.options and len(args) > 0:
            if o.SIMPLIFY_CONSTRAINTS in self.options:
                constraints = [ self.simplify(a) for a in args ]
            else:
                constraints = args

            self._inspect('constraints', BP_BEFORE, added_constraints=constraints)
            self.se.add(*constraints)
            self._inspect('constraints', BP_AFTER)

        if o.ABSTRACT_SOLVER in self.options and len(args) > 0:
            for arg in args:
                if self.se.is_false(arg):
                    self._satisfiable = False
                    return
                if self.se.is_true(arg):
                    continue
                else:
                    # We take the argument, extract a list of constrained SIs out of it (if we could, of course), and
                    # then replace each original SI the intersection of original SI and the constrained one.

                    _, converted = self.se.constraint_to_si(arg)

                    for original_expr, constrained_si in converted:
                        if not original_expr.variables:
                            l.error('Incorrect original_expression to replace in add_constraints(). ' +
                                    'This is due to defects in VSA logics inside claripy. Please report ' +
                                    'to Fish and he will fix it if he\'s free.')
                            continue

                        # FIXME: We are using an expression to intersect a StridedInterval... Is it good?
                        new_expr = original_expr.intersection(constrained_si)
                        self.registers.replace_all(original_expr, new_expr)
                        for _, region in self.memory.regions.items():
                            region.memory.replace_all(original_expr, new_expr)

                        l.debug("SimState.add_constraints: Applied to final state.")
        elif o.SYMBOLIC not in self.options and len(args) > 0:
            for arg in args:
                if self.se.is_false(arg):
                    self._satisfiable = False
                    return

    def BV(self, name, size, explicit_name=None):
        size = self.arch.bits if size is None else size

        self._inspect('symbolic_variable', BP_BEFORE, symbolic_name=name, symbolic_size=size)
        v = self.se.BitVec(name, size, explicit_name=explicit_name)
        self._inspect('symbolic_variable', BP_AFTER, symbolic_expr=v)
        return v

    def BVV(self, value, size=None):
        if type(value) is str:
            v = 0
            for c in value:
                v = v << 8
                v += ord(c)
            size = len(value)*8
            value = v
        size = self.arch.bits if size is None else size
        return self.se.BitVecVal(value, size)

    def StridedInterval(self, name=None, bits=0, stride=None, lower_bound=None, upper_bound=None, to_conv=None):
        return self.se.StridedInterval(name=name,
                                       bits=bits,
                                       stride=stride,
                                       lower_bound=lower_bound,
                                       upper_bound=upper_bound,
                                       to_conv=to_conv)

    def satisfiable(self, **kwargs):
        if o.ABSTRACT_SOLVER in self.options or o.SYMBOLIC not in self.options:
            extra_constraints = kwargs.pop('extra_constraints', ())
            for e in extra_constraints:
                if self.se.is_false(e):
                    return False

            return self._satisfiable
        else:
            return self.se.satisfiable(**kwargs)

    def downsize(self):
        if 'solver_engine' in self.plugins:
            self.se.downsize()

    #
    # Memory helpers
    #

    # Helper function for loading from symbolic memory and tracking constraints
    def _do_load(self, simmem, addr, length, condition=None, fallback=None):
        # do the load and track the constraints
        m,e = simmem.load(addr, length, condition=condition, fallback=fallback)
        self.add_constraints(*e)

        if o.UNINITIALIZED_ACCESS_AWARENESS in self.options and \
                self.uninitialized_access_handler is not None and \
                (m.op == 'Reverse' or m.op == 'I') and \
                hasattr(m.model, 'uninitialized') and \
                m.model.uninitialized:
            if isinstance(simmem, SimAbstractMemory):
                converted_addrs = simmem.normalize_address(addr)
                converted_addrs = [ (region, offset) for region, offset, _, _ in converted_addrs ]
            else:
                converted_addrs = [ addr ]
            self.uninitialized_access_handler(simmem.id, converted_addrs, length, m, self.bbl_addr, self.stmt_idx)

        return m

    # Helper function for storing to symbolic memory and tracking constraints
    def _do_store(self, simmem, addr, content, size=None, condition=None, fallback=None):
        # do the store and track the constraints
        e = simmem.store(addr, content, size=size, condition=condition, fallback=fallback)
        self.add_constraints(*e)
        return e

    #
    # State branching operations
    #

    # Returns a dict that is a copy of all the state's plugins
    def copy_plugins(self):
        return { n: p.copy() for n,p in self.plugins.iteritems() }

    def copy(self):
        '''
        Returns a copy of the state.
        '''

        c_arch = self.arch
        c_plugins = self.copy_plugins()
        state = SimState(arch=c_arch, plugins=c_plugins, options=self.options, mode=self.mode)
        state.abiv = self.abiv
        state.bbl_addr = self.bbl_addr
        state.sim_procedure = self.sim_procedure
        state.stmt_idx = self.stmt_idx
        state.ins_addr = self.ins_addr

        state.uninitialized_access_handler = self.uninitialized_access_handler

        return state

    def merge(self, *others):
        '''
        Merges this state with the other states. Returns the merging result, merged state, and the merge flag.
        :param others: the other states to merge
        :return: (merged state, merge flag, a bool indicating if any merging occured)
        '''
        # TODO: maybe make the length of this smaller? Maybe: math.ceil(math.log(len(others)+1, 2))
        merge_flag = self.se.BitVec("state_merge_%d" % merge_counter.next(), 16)
        merge_values = range(len(others)+1)

        if len(set(o.arch.name for o in others)) != 1:
            raise SimMergeError("Unable to merge due to different architectures.")

        all_plugins = set(self.plugins.keys()) | set.union(*(set(o.plugins.keys()) for o in others))

        merged = self.copy()
        merging_occurred = False

        # plugins
        m_constraints = [ ]
        for p in all_plugins:
            our_plugin = merged.plugins[p] if p in merged.plugins else None
            their_plugins = [ (pl.plugins[p] if p in pl.plugins else None) for pl in others ]

            plugin_classes = (set([our_plugin.__class__]) | set(pl.__class__ for pl in their_plugins)) - set([None.__class__])
            if len(plugin_classes) != 1:
                raise SimMergeError("There are differing plugin classes (%s) for plugin %s" % (plugin_classes, p))
            plugin_class = plugin_classes.pop()

            our_filled_plugin = our_plugin if our_plugin is not None else merged.register_plugin(p, plugin_class())
            their_filled_plugins = [ (tp if tp is not None else t.register_plugin(p, plugin_class())) for t,tp in zip(others, their_plugins) ]

            plugin_state_merged, new_constraints = our_filled_plugin.merge(their_filled_plugins, merge_flag, merge_values)
            if plugin_state_merged:
                l.debug('Merging occured in %s', p)
                merging_occurred = True
            m_constraints += new_constraints
        merged.add_constraints(*m_constraints)

        return merged, merge_flag, merging_occurred

    def widen(self, *others):
        """
        Perform a widening between self and other states
        :param others:
        :return:
        """

        merge_flag = self.se.BitVec("state_merge_%d" % merge_counter.next(), 16)
        merge_values = range(len(others) + 1)

        if len(set(frozenset(o.plugins.keys()) for o in others)) != 1:
            raise SimMergeError("Unable to merge due to different sets of plugins.")
        if len(set(o.arch.name for o in others)) != 1:
            raise SimMergeError("Unable to merge due to different architectures.")

        widened = self.copy()
        widening_occurred = False

        # plugins
        for p in self.plugins:
            plugin_state_widened = widened.plugins[p].widen([_.plugins[p] for _ in others], merge_flag, merge_values)
            if plugin_state_widened:
                l.debug('Widening occured in %s', p)
                widening_occurred = True

        return widened, widening_occurred

    #############################################
    ### Accessors for tmps, registers, memory ###
    #############################################

    def tmp_expr(self, tmp, simplify=False):
        '''
        Returns the Claripy expression of a VEX temp value.

        @param tmp: the number of the tmp
        @param simplify: simplify the tmp before returning it
        @returns a Claripy expression of the tmp
        '''
        self._inspect('tmp_read', BP_BEFORE, tmp_read_num=tmp)
        v = self.log.temps[tmp]
        self._inspect('tmp_read', BP_AFTER, tmp_read_expr=v)
        return v if simplify is False else self.se.simplify(v)

    def store_tmp(self, tmp, content):
        '''
        Stores a Claripy expression in a VEX temp value.

        @param tmp: the number of the tmp
        @param content: a Claripy expression of the content
        '''
        self._inspect('tmp_write', BP_BEFORE, tmp_write_num=tmp, tmp_write_expr=content)

        if tmp not in self.log.temps:
            # Non-symbolic
            self.log.temps[tmp] = content
        else:
            # Symbolic
            self.add_constraints(self.log.temps[tmp] == content)

        self._inspect('tmp_write', BP_AFTER)

    def reg_expr(self, offset, length=None, endness=None, condition=None, fallback=None, simplify=False):
        '''
        Returns the Claripy expression of the content of a register.

        @param offset: the offset or name of the register
        @param length: the length to read. If ommitted, uses the architecture word size
        @param endness: the endianness (little or big) to read with. If ommitted,
                        uses the architecture default.
        @param condition: a condition, for a conditional read
        @param fallback: a fallback Claripy expression, if the Condition ends up being False
        @param simplify: simplify the tmp before returning it
        @returns a Claripy expression representing the read
        '''
        if length is None: length = self.arch.bits / 8
        self._inspect('reg_read', BP_BEFORE, reg_read_offset=offset, reg_read_length=length)

        if type(offset) is str:
            offset,length = self.arch.registers[offset]

        e = self._do_load(self.registers, offset, length, condition=condition, fallback=fallback)

        if endness is None: endness = self.arch.register_endness
        if endness == "Iend_LE": e = e.reversed

        self._inspect('reg_read', BP_AFTER, reg_read_expr=e)
        if simplify or o.SIMPLIFY_REGISTER_READS in self.options:
            e = self.se.simplify(e)
        return e

    def reg_concrete(self, *args, **kwargs):
        '''
        Returns the contents of a register but, if that register is symbolic,
        raises a SimValueError.
        '''
        e = self.reg_expr(*args, **kwargs)
        if self.se.symbolic(e):
            raise SimValueError("target of reg_concrete is symbolic!")
        return self.se.any_int(e)

    def store_reg(self, offset, content, length=None, endness=None, condition=None, fallback=None):
        '''
        Stores content to a register.

        @param offset: the offset or name of the register
        @param content: a Claripy expression to store
        @param length: an optional Claripy expression, representing how much of the content to
                       store. If ommitted, stores the whole thing.
        @param endness: store with the provided endness. If ommitted, uses the architecture default.
        @param condition: a condition, for a conditional store
        @param fallback: the value to store if the condition ends up False.
        '''
        if type(offset) is str:
            offset,length = self.arch.registers[offset]

        if type(content) in (int, long):
            if not length:
                l.info("Length not provided to store_reg with integer content. Assuming bit-width of CPU.")
                length = self.arch.bits / 8
            content = self.se.BitVecVal(content, length * 8)

        if o.SIMPLIFY_REGISTER_WRITES in self.options:
            l.debug("simplifying register write...")
            content = self.simplify(content)

        if endness is None: endness = self.arch.register_endness
        if endness == "Iend_LE": content = content.reversed

        self._inspect('reg_write', BP_BEFORE, reg_write_offset=offset, reg_write_expr=content, reg_write_length=content.size()/8) # pylint: disable=maybe-no-member
        e = self._do_store(self.registers, offset, content, condition=condition, fallback=fallback)
        self._inspect('reg_write', BP_AFTER)

        return e

    def mem_expr(self, addr, length, endness=None, condition=None, fallback=None, simplify=False):
        '''
        Returns the Claripy expression of the content of memory.

        @param addr: a Claripy expression representing the address
        @param length: a Claripy expression representing the length of the read
        @param endness: the endianness (little or big) to read with. If ommitted,
                        does a big endian read.
        @param condition: a condition, for a conditional read
        @param fallback: a fallback Claripy expression, if the Condition ends up being False
        @param simplify: simplify the tmp before returning it
        @returns a Claripy expression representing the read
        '''
        if endness is None: endness = "Iend_BE"

        self._inspect('mem_read', BP_BEFORE, mem_read_address=addr, mem_read_length=length)

        e = self._do_load(self.memory, addr, length, condition=condition, fallback=fallback)
        if endness == "Iend_LE": e = e.reversed

        self._inspect('mem_read', BP_AFTER, mem_read_expr=e)
        if simplify or o.SIMPLIFY_MEMORY_READS in self.options:
            e = self.se.simplify(e)

        return e

    def mem_concrete(self, *args, **kwargs):
        '''
        Returns the contents of a memory but, if the contents are symbolic,
        raises a SimValueError.
        '''
        e = self.mem_expr(*args, **kwargs)
        if self.se.symbolic(e):
            raise SimValueError("target of mem_concrete is symbolic!")
        return self.se.any_int(e)

    def store_mem(self, addr, content, size=None, endness=None, condition=None, fallback=None):
        '''
        Stores content to memory.

        @param addr: a Claripy expression representing the address to store at
        @param content: a Claripy expression to store
        @param size: an optional Claripy expression, representing how much of the content to
                       store. If ommitted, stores the whole thing.
        @param endness: store with the provided endness. If ommitted, uses "Iend_BE" (big endian).
        @param condition: a condition, for a conditional store
        @param fallback: the value to store if the condition ends up False.
        '''
        if o.SIMPLIFY_MEMORY_WRITES in self.options:
            l.debug("simplifying memory write...")
            content = self.simplify(content)

        if endness is None: endness = "Iend_BE"
        if endness == "Iend_LE": content = content.reversed

        self._inspect('mem_write', BP_BEFORE, mem_write_address=addr, mem_write_expr=content, mem_write_length=self.se.BitVecVal(content.size()/8, self.arch.bits) if size is None else size) # pylint: disable=maybe-no-member
        e = self._do_store(self.memory, addr, content, size=size, condition=condition, fallback=fallback)
        self._inspect('mem_write', BP_AFTER)

        return e


    ###############################
    ### Stack operation helpers ###
    ###############################

    @arch_overrideable
    def sp_expr(self):
        '''
        Returns a Claripy expression representing the current value of the stack pointer.
        Equivalent to: state.reg_expr('sp')
        '''
        return self.regs.sp

    @arch_overrideable
    def stack_push(self, thing):
        '''
        Push 'thing' to the stack, writing the thing to memory and adjusting the stack pointer.
        '''
        # increment sp
        sp = self.regs.sp + self.arch.stack_change
        self.regs.sp = sp
        return self.store_mem(sp, thing, endness=self.arch.memory_endness)

    @arch_overrideable
    def stack_pop(self):
        '''
        Pops from the stack and returns the popped thing. The length will be
        the architecture word size.
        '''
        sp = self.regs.sp
        self.regs.sp = sp - self.arch.stack_change
        return self.mem_expr(sp, self.arch.bits / 8, endness=self.arch.memory_endness)

    @arch_overrideable
    def stack_read(self, offset, length, bp=False):
        '''
        Reads length bytes, at an offset into the stack.

        @param offset: the offset from the stack pointer
        @param length: the number of bytes to read
        @param bp: if True, offset from the BP instead of the SP. Default: False
        '''
        sp = self.regs.bp if bp else self.regs.sp
        return self.mem_expr(sp+offset, length, endness=self.arch.memory_endness)

    ###############################
    ### Other helpful functions ###
    ###############################

    def make_concrete(self, expr):
        '''
        Concretizes an expression and updates the state with a constraint
        making it that value. Returns a BitVecVal of the concrete value.
        '''
        if type(expr) in (int, long):
            raise ValueError("expr should not be an int or a long in make_concrete()")

        if not self.se.symbolic(expr):
            return expr

        v = self.se.any_expr(expr)
        self.add_constraints(expr == v)
        return v

    def make_concrete_int(self, expr):
        if type(expr) in (int, long):
            return expr
        return self.se.any_int(self.make_concrete(expr))

    # This handles the preparation of concrete function launches from abstract functions.
    @arch_overrideable
    def prepare_callsite(self, retval, args, convention='wtf'):
        #TODO
        pass

    def dbg_print_stack(self, depth=None, sp=None):
        '''
        Only used for debugging purposes.
        Return the current stack info in formatted string. If depth is None, the
        current stack frame (from sp to bp) will be printed out.
        '''
        var_size = self.arch.bits / 8
        sp_sim = self.regs.sp
        bp_sim = self.regs.bp
        if self.se.symbolic(sp_sim) and sp is None:
            result = "SP is SYMBOLIC"
        elif self.se.symbolic(bp_sim) and depth is None:
            result = "BP is SYMBOLIC"
        else:
            sp_value = sp if sp is not None else self.se.any_int(sp_sim)
            if self.se.symbolic(bp_sim):
                result = "SP = 0x%08x, BP is symbolic\n" % (sp_value)
                bp_value = None
            else:
                bp_value = self.se.any_int(bp_sim)
                result = "SP = 0x%08x, BP = 0x%08x\n" % (sp_value, bp_value)
            if depth is None:
                # bp_value cannot be None here
                depth = (bp_value - sp_value) / var_size + 1 # Print one more value
            pointer_value = sp_value
            for i in range(depth):
                stack_value = self.stack_read(i * var_size, var_size, bp=False)

                if self.se.symbolic(stack_value):
                    concretized_value = "SYMBOLIC - %s" % repr(stack_value)
                else:
                    if len(self.se.any_n_int(stack_value, 2)) == 2:
                        concretized_value = repr(stack_value)
                    else:
                        concretized_value = "0x%08x" % self.se.any_int(stack_value)

                if pointer_value == sp_value:
                    line = "(sp)% 16x | %s" % (pointer_value, concretized_value)
                elif pointer_value == bp_value:
                    line = "(bp)% 16x | %s" % (pointer_value, concretized_value)
                else:
                    line = "% 20x | %s" % (pointer_value, concretized_value)

                pointer_value += var_size
                result += line + "\n"
        return result

    #
    # Other helper methods
    #

    def set_mode(self, mode):
        self.mode = mode
        self.options = set(o.default_options[mode])

from .plugins.symbolic_memory import SimSymbolicMemory
from .plugins.abstract_memory import SimAbstractMemory
from .plugins.named_view import SimRegNameView
from .s_arch import Architectures
from .s_errors import SimMergeError, SimValueError
from .plugins.inspect import BP_AFTER, BP_BEFORE
import simuvex.s_options as o
