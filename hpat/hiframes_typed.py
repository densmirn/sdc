from __future__ import print_function, division, absolute_import

import numpy as np
import numba
from numba import ir, ir_utils, types
from numba.ir_utils import replace_arg_nodes, compile_to_numba_ir, find_topo_order, gen_np_call

import hpat
from hpat.utils import get_definitions
from hpat.hiframes import include_new_blocks, gen_empty_like, get_inner_ir, replace_var_names
from hpat.str_arr_ext import string_array_type, StringArrayType

class HiFramesTyped(object):
    """Analyze and transform hiframes calls after typing"""
    def __init__(self, func_ir, typingctx, typemap, calltypes):
        self.func_ir = func_ir
        self.typingctx = typingctx
        self.typemap = typemap
        self.calltypes = calltypes
        self.df_cols = func_ir.df_cols

    def run(self):
        blocks = self.func_ir.blocks
        call_table, _ = ir_utils.get_call_table(blocks)
        topo_order = find_topo_order(blocks)
        for label in topo_order:
            new_body = []
            for inst in blocks[label].body:
                if isinstance(inst, ir.Assign):
                    out_nodes = self._run_assign(inst, call_table)
                    if isinstance(out_nodes, list):
                        new_body.extend(out_nodes)
                    if isinstance(out_nodes, dict):
                        label = include_new_blocks(blocks, out_nodes, label,
                                                                    new_body)
                        new_body = []
                    if isinstance(out_nodes, tuple):
                        gen_blocks, post_nodes = out_nodes
                        label = include_new_blocks(blocks, gen_blocks, label,
                                                                    new_body)
                        new_body = post_nodes
                else:
                    new_body.append(inst)
            blocks[label].body = new_body

        self.func_ir._definitions = get_definitions(self.func_ir.blocks)
        return

    def _run_assign(self, assign, call_table):
        lhs = assign.target.name
        rhs = assign.value

        if isinstance(rhs, ir.Expr):
            res = self._handle_string_array_expr(lhs, rhs, assign)
            if res is not None:
                return res

            res = self._handle_fix_df_array(lhs, rhs, assign, call_table)
            if res is not None:
                return res

            res = self._handle_df_col_filter(lhs, rhs, assign)
            if res is not None:
                return res

        return [assign]

    def _handle_string_array_expr(self, lhs, rhs, assign):
        # convert str_arr==str into parfor
        if (rhs.op == 'binop'
                and rhs.fn in ['==', '!=']
                and (self.typemap[rhs.lhs.name] == string_array_type
                or self.typemap[rhs.rhs.name] == string_array_type)):
            arg1 = rhs.lhs
            arg2 = rhs.rhs
            arg1_access = 'A'
            arg2_access = 'B'
            len_call = 'A.size'
            if self.typemap[arg1.name] == string_array_type:
                arg1_access = 'A[i]'
            if self.typemap[arg2.name] == string_array_type:
                arg1_access = 'B[i]'
                len_call = 'B.size'
            func_text = 'def f(A, B):\n'
            func_text += '  l = {}\n'.format(len_call)
            func_text += '  S = np.empty(l, dtype=np.bool_)\n'
            func_text += '  for i in numba.parfor.prange(l):\n'
            func_text += '    S[i] = {} {} {}\n'.format(arg1_access, rhs.fn,
                                                                    arg2_access)
            loc_vars = {}
            exec(func_text, {}, loc_vars)
            f = loc_vars['f']
            f_blocks = compile_to_numba_ir(f,
                    {'numba': numba, 'np': np}, self.typingctx,
                    (self.typemap[arg1.name], self.typemap[arg2.name]),
                    self.typemap, self.calltypes).blocks
            replace_arg_nodes(f_blocks[min(f_blocks.keys())], [arg1, arg2])
            # replace == expression with result of parfor (S)
            # S is target of last statement in 1st block of f
            assign.value = f_blocks[min(f_blocks.keys())].body[-2].target
            return (f_blocks, [assign])

        return None

    def _handle_fix_df_array(self, lhs, rhs, assign, call_table):
        # arr = fix_df_array(col) -> arr=col if col is array
        if (rhs.op == 'call'
                and rhs.func.name in call_table
                and call_table[rhs.func.name] ==
                            ['fix_df_array', 'hiframes_api', hpat]
                and isinstance(self.typemap[rhs.args[0].name],
                                    (types.Array, StringArrayType))):
            assign.value = rhs.args[0]
            return [assign]

        return None

    def _handle_df_col_filter(self, lhs_name, rhs, assign):
        # find df['col2'] = df['col1'][arr]
        if (rhs.op=='getitem'
                and rhs.value.name in self.df_cols
                and lhs_name in self.df_cols
                and self.is_bool_arr(rhs.index.name)):
            lhs = assign.target
            in_arr = rhs.value
            index_var = rhs.index
            def f(A, B, ind):
                for i in numba.parfor.prange(len(A)):
                    s = 0
                    if ind[i]:
                        s = B[i]
                    else:
                        s= np.nan
                    A[i] = s
            f_blocks = compile_to_numba_ir(f,
                    {'numba': numba, 'np': np}, self.typingctx,
                    (self.typemap[lhs.name], self.typemap[in_arr.name],
                    self.typemap[index_var.name]),
                    self.typemap, self.calltypes).blocks
            first_block = min(f_blocks.keys())
            replace_arg_nodes(f_blocks[first_block], [lhs, in_arr, index_var])
            alloc_nodes = gen_np_call('empty_like', np.empty_like, lhs, [in_arr],
                        self.typingctx, self.typemap, self.calltypes)
            f_blocks[first_block].body = alloc_nodes + f_blocks[first_block].body
            return f_blocks

    def is_bool_arr(self, varname):
        typ = self.typemap[varname]
        return isinstance(typ, types.npytypes.Array) and typ.dtype==types.bool_
