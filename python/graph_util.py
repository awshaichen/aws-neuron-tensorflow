# Copyright 2020 AWS Neuron. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import os
import signal
import argparse
import time
import tempfile
import json
import struct
import math
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import subprocess
import shlex
import copy
import collections
import pkg_resources
from distutils import spawn
from contextlib import contextmanager
import reprlib
import numpy
from tensorflow.python.util.tf_export import tf_export
from tensorflow.python.util.deprecation import deprecated
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.util import compat
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import importer
from tensorflow.python.framework import graph_util_impl as tf_graph_util
from tensorflow.python.framework import errors
from tensorflow.python.framework.common_shapes import call_cpp_shape_fn
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import nn_ops
from tensorflow.core.framework import graph_pb2
from tensorflow.core.framework.attr_value_pb2 import AttrValue
from tensorflow.python.grappler import tf_optimizer
from tensorflow.core.protobuf import config_pb2
from tensorflow.core.protobuf import rewriter_config_pb2
from tensorflow.python.framework import meta_graph
from tensorflow.neuron.python import graph_def_util as gdu


_NEURON_OP = 'NeuronOp'
_LARGE_CONST_SIZE = 1024


@deprecated(None, 'Please refer to AWS documentation on Neuron integrated TensorFlow 2.0.')
@tf_export('neuron.graph_util.inference_graph_from_session')
def inference_graph_from_session(
        sess=None, input_tensors=None, output_tensors=None,
        shape_feed_dict=None, feed_dict=None, dynamic_batch_size=False,
        protected_op_names=None,
        op_whitelist=None, no_fuse_ops=None, force_fuse_ops=None, minimum_segment_size=None,
        grappler=False, max_num_compilers=None,
        compiler_args=None, compiler_workdir=None, compiler_timeout=None, compiler_recovery=True,
        compiler_verbose=None):
    """Constructs an inference graph from a tensorflow session.

    Generally decomposes into 5 passes:
        1. Convert all variables to constants, `Assign`s to `Identity`s.
        2. Whitelist-based graph partitioning, each subgraph (wrapped in an `NeuronOp`)
            will contain only operations whose types match the types listed in `op_whitelist`.
        3. Shape inference to find shapes for input/output tensors of `NeuronOp` subgraphs.
        4. Call neuron-cc compiler on each `NeuronOp`.
        5. Restore `NeuronOp`s that are failed to compile into their original form.

    Args:
        sess: Active TensorFlow session.
        input_tensors: None or iterable of strings/tensors (unordered). Strings should be
            tensor names. Setting this argument can help when inference starts from some
            arbitrary tensors that are not placeholder tensors.
        output_tensors: None or iterable of strings/tensors (unordered). Strings should be
            tensor names.
        shape_feed_dict: Dict `{str: shape}` used by `shape_inference`.
        feed_dict: Dict `{str: numpy.ndarray}` used by `shape_inference_with_inputs`.
            Optional. If both `shape_feed_dict` and `feed_dict` are unspecified, no shape
            inference will be performed. If only `shape_feed_dict` is specified, will perform
            `shape_inference` only. As long as `feed_dict` is specified, will perform
            `shape_inference` first and then `shape_inference_with_inputs`.
        dynamic_batch_size: Bool that represents whether the inference graph will support
            dynamic batch sizes during inference.
        op_whitelist: Iterable of strings (unordered) representing compilable op names.
        no_fuse_ops: None or iterable of strings (unordered) representing names of ops
            that are forcibly placed on CPU.
        force_fuse_ops: None or iterable of strings (unordered) representing names of ops
            that are forcibly sent to the neuron-cc compiler.
        minimum_segment_size: Integer; minimum number of ops in an `NeuronOp` used by
            `whitelist_partition`.
        max_num_compilers: Integer representing maximum allowed compiler processes.
        compiler_args: List of strings representing compiler arguments. Note that these
            arguments will be applied to all subgraphs generated by whitelist partitioning.
        compiler_workdir: Str representing work directory of the neuron-cc compiler.
        compiler_timeout: Integer representing maximum allowed runtime for the neuron-cc compiler.
        compiler_recovery: Bool representing whether to recovery from neuron-cc compiler failure.

    Returns:
        A `Graph` object that is optimized for running inference on Inferentia.

    Note:
        `input_tensors`, `shape_feed_dict`, and `feed_dict` can all set input tensors, and so
        the latter one will always override the former one.
    """
    if 'NEURON_CC_FLAGS' in os.environ:
        parser = argparse.ArgumentParser()
        parser.add_argument('--must-compile', action='store_true')
        parser.add_argument('--dump-prefix', default=None)
        parser.add_argument('--verbose', type=int, default=None)
        tf_neuron_args, neuron_cc_args = parser.parse_known_args(shlex.split(os.environ['NEURON_CC_FLAGS']))
        if tf_neuron_args.verbose is not None:
            compiler_verbose = tf_neuron_args.verbose
        if tf_neuron_args.must_compile:
            compiler_recovery = False
            if compiler_verbose is None:
                compiler_verbose = 1
            logging.warning('Enabling must-compile according to NEURON_CC_FLAGS environment variable; '
                            'neuron-cc failures will be thrown as exceptions')
        if tf_neuron_args.dump_prefix is not None:
            compiler_workdir = tf_neuron_args.dump_prefix
        if neuron_cc_args:
            if compiler_args is None:
                compiler_args = neuron_cc_args
            else:
                compiler_args.extend(neuron_cc_args)
    if sess is None:
        sess = ops.get_default_session()
    # determine input tensor names and normalize feed_dict/shape_feed_dict keys to tensor names
    if feed_dict is not None:
        feed_dict = {getattr(ts, 'name', ts): value for ts, value in feed_dict.items()}
        input_names = set(feed_dict.keys())
    elif shape_feed_dict is not None:
        shape_feed_dict = {getattr(ts, 'name', ts): value
                           for ts, value in shape_feed_dict.items()}
        input_names = set(shape_feed_dict.keys())
    else:
        input_names = {op.outputs[0].name for op in sess.graph.get_operations()
                                          if op.type == 'Placeholder'}
    if input_tensors is not None:
        input_names = {getattr(ts, 'name', ts) for ts in input_tensors}

    # determine output tensor names
    if output_tensors is None:
        output_ops = _output_ops(sess.graph)
        output_names = {ts.name for op in output_ops for ts in op.outputs}
    else:
        output_names = {getattr(ts, 'name', ts) for ts in output_tensors}
        output_ops = _output_ops(sess.graph, output_names)

    # convert variables to constants
    if protected_op_names is None:
        protected_op_names = set()
    protected_op_names = set(protected_op_names)
    protected_op_names.update(op.name for op in output_ops)
    protected_op_names.update(sess.graph.get_tensor_by_name(name).op.name
                              for name in input_names)
    if feed_dict is not None:
        protected_op_names.update(sess.graph.get_tensor_by_name(name).op.name
                                  for name in feed_dict.keys())
        if shape_feed_dict is None:
            shape_feed_dict = {key: numpy.asarray(val).shape for key, val in feed_dict.items()}
    if shape_feed_dict is not None:
        protected_op_names.update(sess.graph.get_tensor_by_name(name).op.name
                                  for name in shape_feed_dict.keys())

    if grappler:
        with sess.graph.as_default():
            rewriter_config = rewriter_config_pb2.RewriterConfig()
            opt_config = config_pb2.ConfigProto()
            opt_config.graph_options.rewrite_options.CopyFrom(rewriter_config)
            train_op = ops.get_collection_ref(ops.GraphKeys.TRAIN_OP)
            train_op.extend(sess.graph.get_tensor_by_name(name) for name in output_names)
            grappler_metagraph = meta_graph.create_meta_graph_def(graph=sess.graph)
            graph_def = tf_optimizer.OptimizeGraph(opt_config, grappler_metagraph)
    else:
        graph_def = sess.graph.as_graph_def(add_shapes=True)

    # setup datatype maps
    tf_dtype_list = [dtypes.bool, dtypes.complex128, dtypes.float64, dtypes.float32,
                     dtypes.half, dtypes.int64, dtypes.int32, dtypes.complex64,
                     dtypes.string, dtypes.uint32, dtypes.uint64, dtypes.quint8,
                     dtypes.quint16, dtypes.qint8, dtypes.qint16, dtypes.qint32]
    gd_tf_dtype_map = {dtype.as_datatype_enum: dtype for dtype in tf_dtype_list}
    gd_np_dtype_map = {
        dtype.as_datatype_enum: dtype.as_numpy_dtype
        for dtype in tf_dtype_list[:tf_dtype_list.index(dtypes.quint8)]
    }
    gd_np_dtype_map.update({
        dtypes.quint8.as_datatype_enum: numpy.uint8,
        dtypes.quint16.as_datatype_enum: numpy.uint16,
        dtypes.qint8.as_datatype_enum: numpy.int8,
        dtypes.qint16.as_datatype_enum: numpy.int16,
        dtypes.qint32.as_datatype_enum: numpy.int32,
    })

    # record constants
    evaluated_map = {}
    for node in graph_def.node:
        if node.op == 'Const' and node.attr['value'].ByteSize() <= _LARGE_CONST_SIZE:
            gd_tensor = node.attr['value'].tensor
            gd_dtype = gd_tensor.dtype
            if gd_dtype in gd_tf_dtype_map and gd_tf_dtype_map[gd_dtype].is_numpy_compatible:
                tensor_name = '{}:0'.format(node.name)
                np_dtype = gd_np_dtype_map[gd_dtype]
                shape = tensor_shape.TensorShape(gd_tensor.tensor_shape).as_list()
                tensor_content = gd_tensor.tensor_content
                if tensor_content:
                    const_np = numpy.frombuffer(tensor_content, dtype=np_dtype).reshape(shape)
                    evaluated_map[tensor_name] = const_np
                else:
                    val_name = gd_dtype_val_map.get(gd_dtype, None)
                    if val_name is not None:
                        value = getattr(gd_tensor, val_name, [])
                        if len(value) == 1 and np_dtype not in {numpy.float16}:
                            evaluated_map[tensor_name] = numpy.full(shape, value, dtype=np_dtype)
                        elif np_dtype is numpy.float16:
                            value_array = numpy.array(value, dtype=numpy.uint16)
                            if len(value) == 1 and len(shape) != 0:
                                value_array = numpy.broadcast_to(value_array, shape)
                            tensor_content = value_array.tobytes()
                            const_np = numpy.frombuffer(tensor_content, dtype=np_dtype).reshape(shape)
                            evaluated_map[tensor_name] = const_np

    # convert all variables to constants
    with replace_extract_sub_graph():
        graph_def = tf_graph_util.convert_variables_to_constants.__wrapped__(
            sess, graph_def, list(protected_op_names))
    graph = _graph_def_to_graph(graph_def)

    # strip out large constants
    large_constants = erase_large_constants(graph_def)

    # setup op exclusions
    no_fuse_ops = set() if no_fuse_ops is None else set(no_fuse_ops)
    control_op_names = [op.name for op in graph.get_operations() if op._control_outputs]

    # exclude ops with control outputs
    no_fuse_ops.update(control_op_names)

    # exclude ops that are attached to string tensors
    for op in graph.get_operations():
        for ts in op.outputs:
            if ts.dtype == dtypes.string:
                no_fuse_ops.add(ts.op.name)
                no_fuse_ops.update(op.name for op in ts.consumers())

    # infer all tensor shapes and exclude ops that are attached to unknown shape tensors
    if shape_feed_dict is not None:
        shaped_graph = shape_inference(graph, shape_feed_dict, evaluated_map)
        graph_def = graph.as_graph_def()
    else:
        shaped_graph = graph

    # normalize operators
    graph_def = normalize_operators(graph_def, shaped_graph)

    # fuse ops into `NeuronOp`'s and determine tensors that require shapes
    part_graph_def = whitelist_partition(
        graph_def, input_names, output_names, op_whitelist=op_whitelist,
        no_fuse_ops=no_fuse_ops, force_fuse_ops=force_fuse_ops,
        minimum_segment_size=minimum_segment_size)

    # record required subgraph shapes
    part_graph = _graph_def_to_graph(part_graph_def)
    subgraph_shapes = _init_subgraph_shapes(shaped_graph, part_graph)

    # perform an inference to find tensor shapes as a last resort
    # todo: change to hard_shape_inference == True
    if feed_dict is not None:
        subgraph_shapes = shape_inference_with_inputs(sess, graph, feed_dict, subgraph_shapes)

    # call compiler for each `NeuronOp`
    args_dict = {}
    if compiler_args is not None:
        args_dict = {node.name: compiler_args for node in gdu.get_neuron_nodes(part_graph_def)}
    compiled_graph_def = compile_subgraphs(
        part_graph_def, subgraph_shapes, large_constants, workdir=compiler_workdir,
        args_dict=args_dict, timeout=compiler_timeout, max_num_compilers=max_num_compilers,
        verbose=compiler_verbose)

    if dynamic_batch_size:
        compiled_graph_def = mark_batch_axis(compiled_graph_def)

    if compiler_recovery:
        compiled_graph_def = gdu.restore_compiler_failures(compiled_graph_def, graph)
        compiled_graph_def = nchw_to_nhwc(compiled_graph_def)
    for node in compiled_graph_def.node:
        if node.name in large_constants:
            _restore_large_constants(node, large_constants[node.name])

    # try to enable dynamic batch size if possible
    if not dynamic_batch_size:
        compiled_graph_def, dynamic_batch_size = set_dynamic_batch_size(compiled_graph_def)

    # rename NeuronOp's for better visualization
    name_change_map = {}
    for node in gdu.get_neuron_nodes(compiled_graph_def):
        prefix = most_popular_namescope(sn.name for sn in gdu.get_subgraph_def(node).node)
        if not prefix:
            continue
        new_op_name = '/'.join([prefix, node.name])
        num_tensor = len(node.attr['output_names'].list.s)
        for idx in range(num_tensor):
            tensor_name = gdu.format_tensor_name('{}:{}'.format(node.name, idx))
            new_tensor_name = gdu.format_tensor_name('{}:{}'.format(new_op_name, idx))
            name_change_map[tensor_name] = new_tensor_name
        node.name = new_op_name
    for node in compiled_graph_def.node:
        node.input[:] = [name_change_map.get(inp, inp) for inp in node.input]

    # raise exception if NeuronOp is still uncompiled after fallback pass
    uncompiled_node_names = []
    for node in gdu.get_neuron_nodes(compiled_graph_def):
        if not node.attr['executable'].s:
            uncompiled_node_names.append(node.name)
    if uncompiled_node_names:
        raise ValueError('The following subgraphs failed to compile: {}'.format(uncompiled_node_names))

    # execution plan analysis
    compiled_graph_def = set_execution_plan(compiled_graph_def)

    # return a new graph
    compiled_graph = _graph_def_to_graph(compiled_graph_def)
    for name in input_names.union(output_names):
        shape = shaped_graph.get_tensor_by_name(name).shape
        if dynamic_batch_size:
            if shape.rank is not None and shape.rank > 0:
                shape = shape.as_list()
                shape[0] = None
        compiled_graph.get_tensor_by_name(name).set_shape(shape)

    # statistics on number of operations
    num_ops_original = len(sess.graph.get_operations())
    num_ops_tfn, num_ops_on_neuron = compiled_graph_op_counts(compiled_graph)
    with logging_show_info():
        logging.info('Number of operations in TensorFlow session: {}'.format(num_ops_original))
        logging.info('Number of operations after tf.neuron optimizations: {}'.format(num_ops_tfn))
        logging.info('Number of operations placed on Neuron runtime: {}'.format(num_ops_on_neuron))
    if find_neuron_cc() is None:
        logging.warning('***************************************************************')
        logging.warning('')
        logging.warning('  neuron-cc is not found.')
        logging.warning('')
        logging.warning('  To fully optimize TensorFlow model with AWS Neuron, please')
        logging.warning('')
        logging.warning('  install the neuron-cc compiler by "pip install neuron-cc".')
        logging.warning('')
        logging.warning('***************************************************************')
    return compiled_graph


gd_dtype_val_map = {
    dtypes.bool.as_datatype_enum: 'bool_val',
    dtypes.complex128.as_datatype_enum: 'dcomplex_val',
    dtypes.float64.as_datatype_enum: 'double_val',
    dtypes.float32.as_datatype_enum: 'float_val',
    dtypes.half.as_datatype_enum: 'half_val',
    dtypes.int64.as_datatype_enum: 'int64_val',
    dtypes.int32.as_datatype_enum: 'int_val',
    dtypes.complex64.as_datatype_enum: 'scomplex_val',
    dtypes.string.as_datatype_enum: 'string_val',
    dtypes.uint32.as_datatype_enum: 'uint32_val',
    dtypes.uint64.as_datatype_enum: 'uint64_val',
    dtypes.variant.as_datatype_enum: 'variant_val',
}


def erase_large_constants(graph_def):
    # modifies graph_def in-place
    large_constants = {}
    for node in graph_def.node:
        if node.op == 'Const' and node.attr['value'].ByteSize() > _LARGE_CONST_SIZE:
            tensor_content = node.attr['value'].tensor.tensor_content
            if tensor_content:
                large_constants[node.name] = 'tensor_content', tensor_content
                node.attr['value'].tensor.tensor_content = b''
            else:
                gd_dtype = node.attr['dtype'].type
                val_name = gd_dtype_val_map.get(gd_dtype, None)
                if val_name is not None:
                    value = getattr(node.attr['value'].tensor, val_name, None)
                    if value:
                        large_constants[node.name] = val_name, copy.deepcopy(value)
                        node.attr['value'].tensor.ClearField(val_name)
    return large_constants


def find_neuron_cc():
    path = '{}:{}'.format(os.path.dirname(sys.executable), os.environ.get('PATH', ''))
    return spawn.find_executable('neuron-cc', path)


def normalize_operators(graph_def, shaped_graph=None):
    graph = _graph_def_to_graph(graph_def)
    if shaped_graph is None:
        shaped_graph = graph
    for node in graph_def.node:
        if node.op == 'StopGradient':
            node.op = 'Identity'
        elif node.op == 'FusedBatchNormV3':
            op = graph.get_operation_by_name(node.name)
            if all(not ts.consumers() for ts in op.outputs[3:]):
                node.op = 'FusedBatchNorm'
                node.attr.pop('U')
                if '_output_shapes' in node.attr:
                    node.attr['_output_shapes'].list.shape.pop()
        elif node.op == 'AddV2':
            node.op = 'Add'
        elif node.op == 'BatchMatMulV2':  # only change to BatchMatMul if no broadcast
            shaped_op = shaped_graph.get_operation_by_name(node.name)
            shape0, shape1 = shaped_op.inputs[0].shape, shaped_op.inputs[1].shape
            if shape0.rank == shape1.rank and shape0[:-2] == shape1[:-2]:
                node.op = 'BatchMatMul'
    return graph_def


def most_popular_namescope(all_node_names):
    all_splitted = [name.split('/') for name in all_node_names]
    max_level = max(len(splitted) for splitted in all_splitted)
    most_popular_namescope = []
    max_popularity = 0
    for lvl in range(max_level):
        names = [splitted[lvl] for splitted in all_splitted if lvl < len(splitted)]
        (scope, popularity), = collections.Counter(names).most_common(1)
        if popularity >= max_popularity:
            most_popular_namescope.append(scope)
            max_popularity = popularity
        else:
            break
    return '/'.join(most_popular_namescope)


def compiled_graph_op_counts(compiled_graph):
    neuron_ops = [op for op in _neuron_ops(compiled_graph)]
    num_ops_on_neuron = sum(
        len(_get_subgraph(op.node_def).get_operations()) - len(op.get_attr('input_names'))
        for op in neuron_ops if op.get_attr('executable')
    )
    num_ops_tfn = len(compiled_graph.get_operations()) + num_ops_on_neuron - len(neuron_ops)
    return max(num_ops_tfn, 0), max(num_ops_on_neuron, 0)


def _graph_def_to_graph(graph_def):
    graph = ops.Graph()
    with graph.as_default():
        importer.import_graph_def(graph_def, name='')
    return graph


def _neuron_ops(graph):
    return (op for op in graph.get_operations() if op.type == _NEURON_OP)


def _init_subgraph_shapes(graph, part_graph):
    all_tensor_names = {ts.name for op in graph.get_operations() for ts in op.outputs}
    subgraph_shapes = {}
    for op in _neuron_ops(part_graph):
        subgraph = _get_subgraph(op.node_def)
        subgraph_shape_map = {}
        for sg_ts_name, in_tensor in zip(op.get_attr('input_names'), op.inputs):
            sg_tensor = subgraph.get_tensor_by_name(sg_ts_name.decode())
            if sg_tensor.shape.is_fully_defined():
                subgraph_shape_map[sg_tensor.name] = sg_tensor.shape.as_proto()
            else:
                while in_tensor.name not in all_tensor_names:
                    value_index = in_tensor.value_index
                    if in_tensor.op.type == 'IdentityN':
                        in_tensor = in_tensor.op.inputs[value_index]
                    elif in_tensor.op.type == _NEURON_OP:
                        ts_name = in_tensor.op.get_attr('output_names')[value_index]
                        in_tensor = graph.get_tensor_by_name(ts_name.decode())
                        break
                    else:
                        raise TypeError('invalid tensor name {}'.format(sg_tensor.name))
                in_tensor = graph.get_tensor_by_name(in_tensor.name)
                if in_tensor.shape.is_fully_defined():
                    subgraph_shape_map[sg_tensor.name] = in_tensor.shape.as_proto()
                else:
                    subgraph_shape_map[sg_tensor.name] = in_tensor.name
        for sg_ts_name in op.get_attr('output_names'):
            tensor = graph.get_tensor_by_name(sg_ts_name.decode())
            if tensor.shape.is_fully_defined():
                subgraph_shape_map[tensor.name] = tensor.shape.as_proto()
            else:
                subgraph_shape_map[tensor.name] = tensor.name
        subgraph_shapes[op.name] = subgraph_shape_map
    return subgraph_shapes


def _output_ops(graph, output_names=None):
    if output_names is None:
        return {op for op in graph.get_operations()
                   if all(not ts.consumers() for ts in op.outputs)}
    else:
        return {graph.get_tensor_by_name(name).op for name in output_names}


@contextmanager
def logging_show_info():
    verbosity = logging.get_verbosity()
    logging.set_verbosity(logging.INFO)
    try:
        yield
    finally:
        logging.set_verbosity(verbosity)


@contextmanager
def replace_extract_sub_graph():
    extract_sub_graph = tf_graph_util.extract_sub_graph
    tf_graph_util.extract_sub_graph = tf_graph_util.extract_sub_graph.__wrapped__
    try:
        yield
    finally:
        tf_graph_util.extract_sub_graph = extract_sub_graph


def shape_inference(graph, shape_feed_dict, evaluated_map=None):
    """Infer tensor shapes.

    Args:
        graph: A tensorflow `Graph` object.
        shape_feed_dict: Dict `{str: shape}` that maps tensor names to tensor shapes.
            `shape` is a `TensorShapeProto`, a list, or a tuple.

    Returns:
        shape_graph

    Note:
        Can possibly insert new `Const` type ops into `graph` in-place.
    """
    if evaluated_map is None:
        evaluated_map = {}
    shaped_graph = _graph_def_to_graph(graph.as_graph_def(add_shapes=True))

    # set input tensor shapes and get input names
    for key, shape in shape_feed_dict.items():
        shaped_graph.get_tensor_by_name(getattr(key, 'name', key)).set_shape(shape)

    # hack to make call_cpp_shape_fn happy
    for op in shaped_graph.get_operations():
        for ts in op.outputs:
            ts._handle_data = getattr(ts, '_handle_data', None)

    # need to loop number of cycles
    num_cycles = max(1, len([op for op in shaped_graph.get_operations()
                             if op.type in {'NextIteration', 'RefNextIteration'}]))

    def npd(op):
        return op.outputs[0].dtype.as_numpy_dtype

    # try to infer shapes through call_cpp_shape_fn and executing ops
    tensor_array_size_map = {}
    for _ in range(num_cycles):
        for op in shaped_graph.get_operations():
            if op.type == 'Shape' and op.inputs[0].shape.is_fully_defined():
                evaluated_map[op.outputs[0].name] = numpy.array(
                    op.inputs[0].shape.as_list()).astype(npd(op))
            elif op.type == 'StridedSlice' and _is_evaluable(op, evaluated_map):
                input_np, begin_arr, end_arr, strides_arr = [evaluated_map[ts.name]
                                                             for ts in op.inputs]
                ndim = len(begin_arr)
                begin_mask_list = _get_mask(op, 'begin_mask', ndim)
                end_mask_list = _get_mask(op, 'end_mask', ndim)
                ellipsis_mask_list = _get_mask(op, 'ellipsis_mask', ndim)
                new_axis_mask_list = _get_mask(op, 'new_axis_mask', ndim)
                shrink_axis_mask_list = _get_mask(op, 'shrink_axis_mask', ndim)
                slice_list = []
                for (begin, end, strides, begin_mask, end_mask, ellipsis_mask, new_axis_mask,
                        shrink_axis_mask) in zip(
                            begin_arr, end_arr, strides_arr, begin_mask_list, end_mask_list,
                            ellipsis_mask_list, new_axis_mask_list, shrink_axis_mask_list):
                    if ellipsis_mask:
                        slice_list.append(Ellipsis)
                    elif new_axis_mask:
                        slice_list.append(numpy.newaxis)
                    elif shrink_axis_mask:
                        slice_list.append(begin)
                    else:
                        if begin_mask:
                            begin = None
                        if end_mask:
                            end = None
                        slice_list.append(slice(begin, end, strides))
                output_np = input_np[tuple(slice_list)]
                evaluated_map[op.outputs[0].name] = output_np
            elif op.type == 'TensorArrayV3' and op.inputs[0].name in evaluated_map:
                tensor_array_size_map[op.name] = evaluated_map[op.inputs[0].name]
            elif op.type == 'TensorArraySizeV3' and op.inputs[0].op.name in tensor_array_size_map:
                evaluated_map[op.outputs[0].name] = tensor_array_size_map[op.inputs[0].op.name]
            elif op.type == 'Range' and _is_evaluable(op, evaluated_map):
                start, limit, delta = [evaluated_map[ts.name] for ts in op.inputs]
                output_np = numpy.arange(start, limit, delta).astype(npd(op))
                evaluated_map[op.outputs[0].name] = output_np
            elif op.type == 'Pack' and _is_evaluable(op, evaluated_map):
                inputs_np = [evaluated_map[ts.name] for ts in op.inputs]
                output_np = numpy.stack(inputs_np, axis=op.get_attr('axis')).astype(npd(op))
                evaluated_map[op.outputs[0].name] = output_np
            elif op.type == 'Prod' and _is_evaluable(op, evaluated_map):
                input_np, axis_np = [evaluated_map[ts.name] for ts in op.inputs]
                if isinstance(axis_np, numpy.ndarray):
                    axis_np = axis_np.ravel()[0]
                keepdims = op.get_attr('keep_dims')
                output_np = numpy.prod(input_np, axis_np, keepdims=keepdims).astype(npd(op))
                evaluated_map[op.outputs[0].name] = output_np
            elif op.type == 'Mul' and _is_evaluable(op, evaluated_map):
                input0_np, input1_np = [evaluated_map[ts.name] for ts in op.inputs]
                output_np = numpy.asarray(input0_np * input1_np).astype(npd(op))
                evaluated_map[op.outputs[0].name] = output_np
            elif (op.type == 'Reshape' and op.inputs[1].name in evaluated_map
                    and op.inputs[1].op.type != 'Const'):
                shape_np = evaluated_map[op.inputs[1].name]
                with shaped_graph.as_default():
                    new_shape = constant_op.constant_v1(shape_np, name=op.inputs[1].op.name)
                    new_shape._handle_data = None
                    op._update_input(1, new_shape)
            _infer_shape(op)
            if op.type == 'TensorArrayGatherV3' and op.inputs[1].name in evaluated_map:
                output_shape = [evaluated_map[op.inputs[1].name].size]
                output_shape.extend(tensor_shape.TensorShape(op.get_attr('element_shape')).as_list())
                op.outputs[0].set_shape(output_shape)
            elif op.type == 'Fill' and _is_evaluable(op, evaluated_map):
                dims_np, value_np = [evaluated_map[ts.name] for ts in op.inputs]
                output_np = numpy.full(dims_np, value_np).astype(npd(op))
                evaluated_map[op.outputs[0].name] = output_np
                op.outputs[0].set_shape(dims_np)
    return shaped_graph


def _get_mask(op, key, ndim):
    mask_str = bin(op.get_attr(key)).lstrip('0b').zfill(ndim)
    return [int(char) for char in mask_str][::-1]


def _is_evaluable(op, evaluated_map):
    if any(ts.name in evaluated_map for ts in op.outputs):
        return False
    return all(ts.name in evaluated_map for ts in op.inputs)


def _infer_shape(op):
    if any(not ts.shape.is_fully_defined() for ts in op.outputs):
        if op.type == 'Enter':
            shape_list = [inp.shape.as_proto() for inp in op.inputs]
        else:
            shape_list = call_cpp_shape_fn(op)['shapes']
        for ts, shape in zip(op.outputs, shape_list):
            if not ts.shape.is_fully_defined():
                ts.set_shape(shape)


def shape_inference_with_inputs(sess, graph, feed_dict, subgraph_shapes):
    """Infer tensor shapes by running inference.

    Args:
        graph: A tensorflow `Graph` object.
        feed_dict: dict `{str: numpy.ndarray}` that maps tensor names to input
            numpy arrays, as used in a full inference with session.run.
        subgraph_shapes: Nested dict `{str: {str: <str or TensorShapeProto>}}`
            1st level key: subgraph name
            2nd level key: subgraph tensor name
            2nd level value: tensor shape as `TensorShapeProto` or tensor name
                in the original graph (before `whitelist_partition`).

    Returns:
        A dict in the same format as subgraph_shapes, with 2nd level values possibly
            filled by `TensorShapeProto`s inferred from the original graph.
    """
    need_shape = _need_shape_tensors(subgraph_shapes, graph)
    need_shape = [sess.graph.get_tensor_by_name(ts.name) for ts in need_shape]
    need_shape_infer = []
    for tensor in need_shape:
        if sess.graph.is_fetchable(tensor.op):
            need_shape_infer.append(tensor)
        else:
            logging.warning('cannot infer shape for tensor {}; it is recommended '
                            'to provide its shape in shape_feed_dict'.format(tensor))
    if need_shape_infer:
        tensors_repr = reprlib.Repr()
        tensors_repr.maxlist = 8
        tensors_repr.maxother = 80
        ts_repr_str = tensors_repr.repr(need_shape_infer)
        logging.warning('running inference to find shape for {} ({} tensors)'
                        .format(ts_repr_str, len(need_shape_infer)))
        feed_dict = {getattr(key, 'name', key): val for key, val in feed_dict.items()}
        need_shape_infer_np = sess.run(need_shape_infer, feed_dict)
        for tensor, value in zip(need_shape_infer, need_shape_infer_np):
            tensor = graph.get_tensor_by_name(tensor.name)
            if hasattr(value, 'shape'):
                tensor.set_shape(value.shape)
            elif numpy.isscalar(value):
                tensor.set_shape(tuple())
    return _new_subgraph_shapes(subgraph_shapes, graph)


def _need_shape_tensors(subgraph_shapes, graph):
    need_shape_tensors = []
    visited_tensor_name_set = set()
    for ts_name_map in subgraph_shapes.values():
        for name in ts_name_map.values():
            if isinstance(name, str) and name not in visited_tensor_name_set:
                need_shape_tensors.append(graph.get_tensor_by_name(name))
                visited_tensor_name_set.add(name)
    return need_shape_tensors


def _new_subgraph_shapes(subgraph_shapes, graph):
    new_subgraph_shapes = {}
    for key, sg_ts_name_map in subgraph_shapes.items():
        new_sg_ts_name_map = {}
        for sg_ts_name, value in sg_ts_name_map.items():
            if isinstance(value, str):
                new_shape = graph.get_tensor_by_name(value).shape
                if new_shape.is_fully_defined():
                    value = new_shape.as_proto()
            new_sg_ts_name_map[sg_ts_name] = value
        new_subgraph_shapes[key] = new_sg_ts_name_map
    return new_subgraph_shapes


def whitelist_partition(graph_def, input_tensors=None, output_tensors=None,
                        op_whitelist=None, no_fuse_ops=None, force_fuse_ops=None,
                        minimum_segment_size=None):
    """Partitions a `GraphDef` proto according to a TensorFlow op whitelist and
    fuses each whitelisted subgraph into an `NeuronOp`.

    Args:
        graph_def: input `GraphDef` proto.
        input_tensors: None or iterable of strings/tensors (unordered). Strings should be
            tensor names.
        output_tensors: None or iterable of strings/tensors (unordered). Strings should be
            tensor names.
        op_whitelist: None or iterable of strings (unordered) representing
            whitelisted op type names.
        no_fuse_ops: None or iterable of strings (unordered) representing
            names of ops that will stay unfused.
        force_fuse_ops: None or iterable of strings (unordered) representing
            names of ops that will be forcibly fused into `NeuronOp`.
        minimum_segment_size: int; minimum number of ops in an `NeuronOp`.

    Returns:
        A `GraphDef` proto with whitelisted subgraphs fused as `NeuronOp`s.
    """
    for node in graph_def.node:
        if '_output_shapes' in node.attr:
            node.attr['_aws_neuron_inferred_shapes'].CopyFrom(node.attr['_output_shapes'])
    graph = _graph_def_to_graph(graph_def)
    if input_tensors is None:
        input_tensors = {op.outputs[0] for op in graph.get_operations()
                                       if op.type == 'Placeholder'}
    if output_tensors is None:
        output_tensors = {ts for op in _output_ops(graph) for ts in op.outputs}
    if op_whitelist is None:
        neuron_cc = find_neuron_cc()
        if neuron_cc is None:
            return graph_def
        else:
            command = [neuron_cc, 'list-operators', '--framework', 'TENSORFLOW']
            try:
                op_whitelist = {op_type.strip() for op_type in subprocess.check_output(command).decode()[:-1].split('\n')}
            except subprocess.CalledProcessError:
                logging.warning('neuron-cc is not behaving correctly. Please check neuron-cc '
                                'installation, or reinstall by "pip install --force neuron-cc".')
                return graph_def
            op_whitelist.discard('Placeholder')
            op_whitelist.discard('IdentityN')
            op_whitelist.add('SquaredDifference')
    if no_fuse_ops is None:
        no_fuse_ops = []
    if force_fuse_ops is None:
        force_fuse_ops = []
    if minimum_segment_size is None:
        num_ops = len([node for node in graph_def.node if node.op != 'Placeholder'])
        minimum_segment_size = min(2, max(1, num_ops))
    opt_config = config_pb2.ConfigProto()
    rewriter_config = opt_config.graph_options.rewrite_options
    rewriter_config.meta_optimizer_iterations = 1
    rewriter_config.min_graph_nodes = 2
    rewriter_config.optimizers.append('')
    fuser_config = rewriter_config.custom_optimizers.add()
    fuser_config.name = 'aws_neuron_fuse_supported_operators'
    param_map = fuser_config.parameter_map
    param_map['inputs'].list.s.extend(compat.as_bytes(getattr(ts, 'name', ts)) for ts in input_tensors)
    output_names = [compat.as_str(getattr(ts, 'name', ts)) for ts in output_tensors]
    param_map['outputs'].list.s.extend(compat.as_bytes(name) for name in output_names)
    param_map['minimum_segment_size'].i = minimum_segment_size
    param_map['op_whitelist'].list.s.extend(compat.as_bytes(item) for item in op_whitelist)
    param_map['no_fuse_ops'].list.s.extend(compat.as_bytes(getattr(item, 'name', item)) for item in no_fuse_ops)
    param_map['force_fuse_ops'].list.s.extend(compat.as_bytes(getattr(item, 'name', item)) for item in force_fuse_ops)
    graph.get_collection_ref(ops.GraphKeys.TRAIN_OP).extend(graph.get_tensor_by_name(name) for name in output_names)
    meta_graph_def = meta_graph.create_meta_graph_def(graph=graph)
    graph_def = tf_optimizer.OptimizeGraph(opt_config, meta_graph_def)

    # add subgraph's control input to `NeuronOp`'s control input
    all_op_names = {op.name for op in graph.get_operations()}
    post_part_node_names = {node.name for node in graph_def.node}
    for node in gdu.get_neuron_nodes(graph_def):
        for sg_node in gdu.get_subgraph_def(node).node:
            if sg_node.name in all_op_names:
                op_original = graph.get_operation_by_name(sg_node.name)
                for control_input in op_original.control_inputs:
                    if control_input.name in post_part_node_names:
                        node.input.append('^{}'.format(control_input.name))
    return graph_def


def _get_subgraph(node):
    return _graph_def_to_graph(gdu.get_subgraph_def(node))


def compile_subgraphs(graph_def, subgraph_shapes=None, large_constants=None,
                      workdir=None, args_dict=None, timeout=None, max_num_compilers=None,
                      verbose=None):
    """Compile `NeuronOp`s in a `GraphDef` proto.

    Args:
        graph_def: Input `GraphDef` proto that contains `NeuronOp`s.
        subgraph_shapes: Nested dict `{str: {str: <str or TensorShapeProto>}}`
            1st level key: subgraph name
            2nd level key: subgraph tensor name
            2nd level value: tensor shape as `TensorShapeProto` or tensor name
                in the original graph (before `whitelist_partition`).
        workdir: None or path-like representing the working directory used by the compiler;
            if None, will use `tempfile` to create a temporary workdir for each subgraph,
            else will create and use 'workdir/op_name' for each subgraph.
        args_dict: Dict `{str: list}` that maps NeuronOp names to compiler arguments;
            compiler arguments should be a list of strings, as used in `subprocess.run`.
        timeout: Integer representing timeout limit for the compiler. Default: 18000.
        max_num_compilers: Integer representing maximum allowed compiler processes.
            Default is number of cpu cores.

    Returns:
        A `GraphDef` proto with `NeuronOp`s already compiled.
    """
    if all(node.op != _NEURON_OP for node in graph_def.node):
        return graph_def
    subgraph_compilers = {}
    if workdir is None:
        workdir_obj = tempfile.TemporaryDirectory()
        workdir_base = workdir_obj.name
    else:
        workdir_base = os.path.abspath(workdir)
    if timeout is None:
        timeout = 18000
    Compiler = collections.namedtuple('Compiler', 'command verbose workdir_path subgraph_info')
    _neuron_cc_input_name = 'graph_def.pb'
    _neuron_executable_name = 'graph_def.neff'
    neuron_cc = find_neuron_cc()
    if neuron_cc is None:
        return graph_def
    subgraph_info_format = '{{subgraph {} with input tensors {}, output tensors {}}}'.format
    for node in gdu.get_neuron_nodes(graph_def):
        if len(node.attr['input_names'].list.s) == 0 or len(node.attr['output_names'].list.s) == 0:
            continue
        subgraph = _get_subgraph(node)
        input_tensors = [subgraph.get_tensor_by_name(name.decode()) for name in node.attr['input_names'].list.s]
        output_tensors = [subgraph.get_tensor_by_name(name.decode()) for name in node.attr['output_names'].list.s]
        subgraph_info = subgraph_info_format(node.name, input_tensors, output_tensors)
        io_config_json = _io_config(node, subgraph, subgraph_shapes)
        if io_config_json is None:
            logging.warning('Not fusing subgraph {}: --io-config error'.format(node.name))
            continue
        if _get_shapes(node, 'output_names', subgraph_shapes, subgraph) is None:
            logging.warning('Cannot infer tensor shapes for subgraph {}'.format(node.name))
            continue
        if subgraph_shapes is not None:
            for tensor in input_tensors:
                tensor.set_shape(subgraph_shapes[node.name][tensor.name])
            for tensor in output_tensors:
                tensor.set_shape(subgraph_shapes[node.name][tensor.name])
        subgraph_info = subgraph_info_format(node.name, input_tensors, output_tensors)
        subgraph_def = gdu.get_subgraph_def(node)
        for sgn in subgraph_def.node:
            sgn.attr.pop('_aws_neuron_inferred_shapes', None)
        if large_constants is not None:
            for sgn in subgraph_def.node:
                if sgn.name in large_constants:
                    _restore_large_constants(sgn, large_constants[sgn.name])
        workdir_path = os.path.join(workdir_base, node.name)
        os.makedirs(workdir_path, exist_ok=True)
        input_path = os.path.join(workdir_path, _neuron_cc_input_name)
        with open(input_path, 'wb') as f:
            f.write(subgraph_def.SerializeToString())
        command = [neuron_cc, 'compile', input_path, '--framework', 'TENSORFLOW',
                   '--pipeline', 'compile', 'SaveTemps',
                   '--output', os.path.join(workdir_path, _neuron_executable_name)]
        command.extend(['--io-config', io_config_json])
        if args_dict is not None:
            extend_args = args_dict.get(node.name, [])
            if isinstance(extend_args, (str, bytes)):
                extend_args = [extend_args]
            command.extend(extend_args)
        if verbose is not None:
            command.extend(['--verbose', str(verbose)])
        subgraph_compilers[node.name] = Compiler(command, verbose, workdir_path, subgraph_info)
    if max_num_compilers is None:
        num_cpu = multiprocessing.cpu_count()
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if 'MemAvailable' in line:
                        available_mem_in_kb = int(line.split()[1])
                        break
            num_mem_gb = int(available_mem_in_kb / 4e6)  # 4 GB memory for each neuron-cc process
            max_num_compilers = max(1, min(num_cpu, num_mem_gb))
        except:
            max_num_compilers = num_cpu
    with ThreadPoolExecutor(max_workers=max_num_compilers) as executor:
        compiler_returns = {
            node_name: executor.submit(_fork_compiler, subgraph_compilers, node_name, timeout)
            for node_name in subgraph_compilers.keys()
        }
        compiler_returns = {key: value.result() for key, value in compiler_returns.items()}
    for node_name in subgraph_compilers.keys():
        if not compiler_returns[node_name]:
            subgraph_compilers[node_name] = None

    # fill NeuronOp properties
    for node in gdu.get_neuron_nodes(graph_def):
        node.attr['input_batch_axis'].list.i[:] = [-1 for _ in node.attr['input_names'].list.s]
        node.attr['output_batch_axis'].list.i[:] = [-1 for _ in node.attr['output_names'].list.s]
        if subgraph_compilers.get(node.name, None) is None:
            continue
        # todo: change when there is a better way to return shapes
        subgraph = _get_subgraph(node)
        input_shapes = _get_shapes(node, 'input_names', subgraph_shapes, subgraph)
        output_shapes = _get_shapes(node, 'output_names', subgraph_shapes, subgraph)
        if input_shapes is None or output_shapes is None:
            logging.warning('Cannot infer tensor shapes for subgraph {}'.format(node.name))
            continue
        node.attr['input_shapes'].list.CopyFrom(AttrValue.ListValue(shape=input_shapes))
        node.attr['output_shapes'].list.CopyFrom(AttrValue.ListValue(shape=output_shapes))
        workdir_path = subgraph_compilers[node.name].workdir_path
        executable_path = os.path.join(workdir_path, _neuron_executable_name)
        with open(executable_path, 'rb') as f:
            node.attr['executable'].s = f.read()
    return graph_def


def _restore_large_constants(node, stored_large_constants):
    attr_name, attr_value = stored_large_constants
    if attr_name == 'tensor_content':
        node.attr['value'].tensor.tensor_content = attr_value
    else:
        getattr(node.attr['value'].tensor, attr_name)[:] = attr_value


def _fork_compiler(subgraph_compilers, node_name, timeout):
    compiler = subgraph_compilers[node_name]
    if compiler is None:
        return None
    command, verbose, workdir_path, subgraph_info = compiler
    logfile = os.path.join(workdir_path, 'graph_def.neuron-cc.log')
    info_string = 'fusing subgraph {} with neuron-cc'.format(subgraph_info)
    if not verbose:
        info_string = '{}; you may check progress by inspecting file {}'.format(info_string, logfile)
    with logging_show_info():
        logging.info(info_string)
    if verbose:
        proc = subprocess.Popen(command, cwd=workdir_path)
        returncode = _wait_compiler(proc, timeout)
    else:
        with open(logfile, 'w') as logfd:
            proc = subprocess.Popen(command, cwd=workdir_path, stdout=logfd, stderr=logfd)
            returncode = _wait_compiler(proc, timeout)
    if returncode != 0:
        logging.warning("Failed to fuse subgraph {} with '{}'".format(subgraph_info, subprocess.list2cmdline(command)))
        if not verbose:
            logging.warning("neuron-cc error message:")
            full_neuron_cc_log = []
            neuron_cc_error_message = []
            found_error = False
            with open(logfile, 'r') as f:
                for line in f:
                    if len(full_neuron_cc_log) < 20:
                        full_neuron_cc_log.append(line)
                    if '[neuron-cc]: *************************************************' in line:
                        found_error = True
                    if found_error and 'Artifacts stored' not in line:
                        neuron_cc_error_message.append(line)
            if len(full_neuron_cc_log) < 20:
                logging.warning(''.join(full_neuron_cc_log))
            else:
                logging.warning(''.join(neuron_cc_error_message))
            io_config_index = command.index('--io-config')
            with open(os.path.join(workdir_path, 'io-config.json'), 'w') as f:
                f.write(command[io_config_index+1])
            if neuron_cc_error_message:
                with open(os.path.join(workdir_path, 'neuron-cc-error.txt'), 'w') as f:
                    f.write('\n'.join(neuron_cc_error_message))
        return None
    return True


def _wait_compiler(proc, timeout):
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGKILL)
        return None
    return proc.returncode


def get_model_config(executable):
    default_model_config = [-1, -1, -1, 10]
    if not executable:
        return default_model_config
    tuple_cores = _neff_get_cores_from_executable(executable)
    if tuple_cores is None:
        return default_model_config
    opt_num_cores, min_num_cores = tuple_cores
    est_infer_timeout = len(executable) / 1e8
    infer_timeout = max(est_infer_timeout, 10)
    model_config = [-1, opt_num_cores, opt_num_cores, infer_timeout]
    return model_config


_NC_HEADER_SIZE = 544
def _neff_get_cores(neff_filename):
    with open(neff_filename, mode='rb') as f:
        header = f.read(_NC_HEADER_SIZE)
    if len(header) != _NC_HEADER_SIZE:
        return None
    return _neff_get_cores_from_executable(header)


def _neff_get_cores_from_executable(executable):
    header = executable[:_NC_HEADER_SIZE]
    if len(header) != _NC_HEADER_SIZE:
        return None
    info = struct.unpack('168xI304xI64B', header)
    if len(info) != 66:  # 1 + 1 + 64
        return None
    opt_num_cores = info[0]
    if opt_num_cores <= 0 or opt_num_cores > 64:
        return None
    min_num_cores = max(info[2:])
    if min_num_cores <= 0 or min_num_cores > 64:
        return None
    return opt_num_cores, min_num_cores


def mark_batch_axis(compiled_graph_def):
    for node in gdu.get_neuron_nodes(compiled_graph_def):
        subgraph = _get_subgraph(node)
        node.attr['input_batch_axis'].list.i[:] = _batch_axis(node, subgraph, 'input_names')
        node.attr['output_batch_axis'].list.i[:] = _batch_axis(node, subgraph, 'output_names')
    return compiled_graph_def


def _batch_axis(node, subgraph, names_key):
    return [_one_batch_axis(subgraph, name) for name in node.attr[names_key].list.s]


def _one_batch_axis(subgraph, name):
    shape = subgraph.get_tensor_by_name(name.decode()).shape
    if shape.rank is None:
        return 0
    return 0 if len(shape) > 0 and tensor_shape.dimension_value(shape[0]) is None else -1


def _io_config(node, subgraph, subgraph_shapes=None):
    inputs = {}
    for name in node.attr['input_names'].list.s:
        name = name.decode()
        tensor = subgraph.get_tensor_by_name(name)
        if subgraph_shapes is None:
            shape = tensor.shape
        else:
            shape = subgraph_shapes[node.name][tensor.name]
            shape = tensor.shape if isinstance(shape, str) else tensor_shape.TensorShape(shape)
        if not shape.is_fully_defined():
            logging.warning('subgraph {}, tensor {}: invalid shape {}'
                            .format(node.name, name, shape))
            return None
        inputs[tensor.name] = [shape.as_list(), tensor.dtype.name]
    outputs = [name.decode() for name in node.attr['output_names'].list.s]
    return json.dumps({'inputs': inputs, 'outputs': outputs})


def _get_shapes(node, names_key, subgraph_shapes, subgraph):
    shapes = []
    for name in node.attr[names_key].list.s:
        name = name.decode()
        if subgraph_shapes is None:
            shape = subgraph.get_tensor_by_name(name).shape
        else:
            shape = subgraph_shapes[node.name][name]
            if isinstance(shape, str):
                shapes = None
                break
            shape = tensor_shape.TensorShape(shape)
        if not shape.is_fully_defined():
            shapes = None
            break
        shapes.append(shape.as_proto())
    return shapes


def nchw_to_nhwc(graph_def):
    """Convert data formats of all Conv2D/MaxPool/AvgPool ops to NCHW and insert transposes
    """
    remove_node_names = set()
    node_rename_map = {}
    graph = _graph_def_to_graph(graph_def)
    perm_to_nhwc = [0, 2, 3, 1]

    def get_nhwc_attr(name):
        attribute = op.get_attr(name)
        if isinstance(attribute, list) and len(attribute) == 4:
            return [attribute[idx] for idx in perm_to_nhwc]
        else:
            return attribute

    with graph.as_default():
        func_map = {
            'Conv2D': nn_ops.conv2d,
            'MaxPool': nn_ops.max_pool,
            'AvgPool': nn_ops.avg_pool,
        }
        for op in graph.get_operations():
            if op.type in func_map and op.get_attr('data_format') == b'NCHW':
                if op.type == 'Conv2D':
                    padding = op.get_attr('padding')
                    if padding == b'EXPLICIT':
                        explicit = op.get_attr('explicit_paddings')
                        padding = [explicit[2*idx:2*idx+2] for idx in perm_to_nhwc]
                    kwargs = dict(filters=op.inputs[1], dilations=get_nhwc_attr('dilations'),
                                  padding=padding, strides=get_nhwc_attr('strides'))
                elif op.type in {'MaxPool', 'AvgPool'}:
                    kwargs = dict(ksize=get_nhwc_attr('ksize'), padding=op.get_attr('padding'),
                                  strides=get_nhwc_attr('strides'))
                else:
                    continue
                input_nchw = op.inputs[0]
                with ops.name_scope(op.name):
                    input_nhwc = array_ops.transpose(input_nchw, perm_to_nhwc)
                    tensor_nhwc = func_map[op.type](input_nhwc, **kwargs)
                    tensor_nchw = array_ops.transpose(tensor_nhwc, [0, 3, 1, 2])
                remove_node_names.add(op.name)
                node_rename_map[tensor_nchw.op.name] = op.name
    temp_graph_def = graph.as_graph_def()
    graph_def = graph_pb2.GraphDef()
    graph_def.node.MergeFrom(
        node for node in temp_graph_def.node if node.name not in remove_node_names)
    for node in graph_def.node:
        if node.name in node_rename_map:
            node.name = node_rename_map[node.name]
    return graph_def


def set_dynamic_batch_size(compiled_graph_def):
    dbs = DynamicBatchSizeHelper()
    subgraph_enable_map = {}
    for node in gdu.get_neuron_nodes(compiled_graph_def):
        subgraph = _get_subgraph(node)
        input_names = [name.decode() for name in node.attr['input_names'].list.s]
        output_names = [name.decode() for name in node.attr['output_names'].list.s]
        tensor_dynamic_map = {}
        for name in input_names:
            shape = subgraph.get_tensor_by_name(name).shape
            tensor_dynamic_map[name] = shape.rank is None or (len(shape) > 0 and shape.as_list()[0] is None)
        for op in subgraph.get_operations():
            inputs, outputs = dbs.dynamic_inputs_outputs(op)
            if all(tensor_dynamic_map.get(ts.name, False) for ts in inputs):
                tensor_dynamic_map.update((ts.name, True) for ts in outputs)
        subgraph_enable_map[node.name] = all(tensor_dynamic_map.get(name, False) for name in input_names + output_names)
    dynamic_batch_size = subgraph_enable_map and all(
        subgraph_enable_map.get(node.name, False) for node in gdu.get_neuron_nodes(compiled_graph_def))
    if dynamic_batch_size:
        for node in gdu.get_neuron_nodes(compiled_graph_def):
            subgraph = _get_subgraph(node)
            node.attr['input_batch_axis'].list.i[:] = _batch_axis(node, subgraph, 'input_names')
            node.attr['output_batch_axis'].list.i[:] = _batch_axis(node, subgraph, 'output_names')
    return compiled_graph_def, dynamic_batch_size


class DynamicBatchSizeHelper:

    unary_ops = {
        'Bitcast', 'Identity', 'Abs', 'Acos', 'Acosh', 'Asin', 'Asinh', 'Atan', 'Atan2',
        'Atanh', 'BesselI0e', 'BesselI1e', 'Cast', 'Ceil', 'Cos', 'Cosh', 'Digamma',
        'Erf', 'Erfc', 'Exp', 'Expm1', 'Floor', 'FloorDiv', 'FloorMod', 'Inv',
        'IsFinite', 'IsInf', 'IsNan', 'Lgamma', 'Log', 'Log1p', 'Mod', 'Neg', 'Pow',
        'Reciprocal', 'Rint', 'Round', 'Rsqrt', 'Sigmoid', 'Sign', 'Sin', 'Sinh', 'Sqrt',
        'Square', 'Tan', 'Tanh', 'Elu', 'Relu', 'Relu6', 'Selu', 'Softplus', 'Softsign',
        'LogSoftmax', 'Softmax',
    }
    binary_broadcast_ops = {
        'Add', 'AddV2', 'Div', 'DivNoNan', 'Equal', 'Greater', 'GreaterEqual',
        'Less', 'LessEqual', 'LogicalAnd', 'LogicalNot', 'LogicalOr', 'Maximum', 'Minimum',
        'Mul', 'MulNoNan', 'NotEqual', 'RealDiv', 'SquaredDifference', 'Subtract',
        'TruncateDiv', 'TruncateMod', 'Xdivy', 'Xlogy',
    }
    reduce_axis_ops = {
        'ArgMax', 'ArgMin', 'EuclideanNorm', 'Max', 'Mean', 'Min', 'Prod', 'Sum',
    }
    pseudo_unary_ops = {
        'Pad', 'PadV2', 'ClipByValue', 'AvgPool', 'AvgPool3D', 'BiasAdd',
        'Conv2D', 'Conv3D', 'DepthwiseConv2dNative', 'Dilation2D',
        'FractionalAvgPool', 'FractionalMaxPool', 'FusedBatchNorm', 'FusedBatchNormV2', 'FusedBatchNormV3',
        'FusedPadConv2D', 'FusedResizeAndPadConv2D', 'MaxPool', 'MaxPoolV2', 'MaxPool3D',
    }

    def dynamic_inputs_outputs(self, op):
        if op.type in DynamicBatchSizeHelper.unary_ops:
            return list(op.inputs), op.outputs
        elif op.type in DynamicBatchSizeHelper.binary_broadcast_ops:
            shape0, shape1 = [ts.shape for ts in op.inputs]
            if shape0.rank is None or shape1.rank is None:
                return [], []
            if shape0.rank > shape1.rank:
                return [op.inputs[0]], op.outputs
            elif shape0.rank < shape1.rank:
                return [op.inputs[1]], op.outputs
            else:  # same rank
                inputs = []
                if len(shape0) > 0 and shape0.as_list()[0] is None:
                    inputs.append(op.inputs[0])
                if len(shape1) > 0 and shape0.as_list()[0] is None:
                    inputs.append(op.inputs[1])
                return inputs, op.outputs
        elif op.type in DynamicBatchSizeHelper.reduce_axis_ops:
            axis_op = op.inputs[-1].op
            if axis_op.type == 'Const':
                axis_list = _get_int32_values(axis_op)
                if axis_list and 0 not in axis_list:
                    return list(op.inputs[:-1]), op.outputs
        elif op.type in DynamicBatchSizeHelper.pseudo_unary_ops:
            return list(op.inputs[:1]), op.outputs
        elif op.type in {'Concat', 'ConcatV2'}:
            axis_op = op.inputs[-1].op
            if axis_op.type == 'Const':
                axis_list = _get_int32_values(axis_op)
                if axis_list and 0 not in axis_list:
                    return list(op.inputs[:-1]), op.outputs
        elif op.type == 'ExpandDims':
            pass
        elif op.type == 'Stack':
            pass
        elif op.type in {'BatchMatMul', 'BatchMatMulV2'}:
            pass
        elif op.type == 'Cumprod':
            pass
        elif op.type == 'Cumsum':
            pass
        elif op.type == 'MatMul':
            if not op.node_def.attr['transpose_a'].b:
                return list(op.inputs[:1]), op.outputs
        elif op.type == 'Slice':
            pass
        elif op.type == 'StridedSlice':
            pass
        elif op.type == 'Shape':
            pass
        elif op.type == 'Reshape':
            pass
        elif op.type == 'Squeeze':
            pass
        elif op.type == 'Transpose':
            pass
        elif op.type == 'Unstack':
            pass
        return [], []


def _get_int32_values(const_op):
    tensor_def = const_op.node_def.attr['value'].tensor
    dtype = dtypes._INTERN_TABLE[tensor_def.dtype]
    if dtype is not dtypes.int32:
        return []
    if tensor_def.tensor_content:
        return list(numpy.frombuffer(tensor_def.tensor_content, dtype=dtype.as_numpy_dtype))
    else:
        return tensor_def.int_val


def set_execution_plan(compiled_graph_def):
    # scan to get num neuroncores and total number of bytes of input and output tensors
    default_io_buffer_size = 128 * 1024 * 1024
    cpu_extra_ninfer = 3
    num_cores_tuple_map = {}
    mis_config = False
    neuron_nodes = list(gdu.get_neuron_nodes(compiled_graph_def))
    for node in neuron_nodes:
        num_cores_tuple = _neff_get_cores_from_executable(node.attr['executable'].s)
        if num_cores_tuple is None:
            mis_config = True
        else:
            opt_num_cores, _ = num_cores_tuple
            num_cores_tuple_map[node.name] = num_cores_tuple
    total_io_bytes = 0
    for node in neuron_nodes:
        model_io_bytes = 0
        for enum, shape in zip(node.attr['input_dtypes'].list.type, node.attr['input_shapes'].list.shape):
            model_io_bytes += dtypes._INTERN_TABLE[enum].size * numpy.prod([dim.size for dim in shape.dim])
        for enum, shape in zip(node.attr['output_dtypes'].list.type, node.attr['output_shapes'].list.shape):
            model_io_bytes += dtypes._INTERN_TABLE[enum].size * numpy.prod([dim.size for dim in shape.dim])
        if node.name not in num_cores_tuple_map:
            total_io_bytes = 0
            break
        this_opt_num_cores, _ = num_cores_tuple_map[node.name]
        total_io_bytes += model_io_bytes * (this_opt_num_cores + cpu_extra_ninfer)  # io size * ninfer
    max_num_duplicates = 1
    if total_io_bytes > 0:
        max_num_duplicates = math.floor(default_io_buffer_size / total_io_bytes)
        max_num_duplicates = min(max_num_duplicates, 4)  # use at most 1 MLA (4 cores) by default
        max_num_duplicates = max(max_num_duplicates, 1)
    if mis_config or not num_cores_tuple_map:
        global_opt_num_cores = -1
        max_num_duplicates = 1
    else:
        global_opt_num_cores = max(opt_nc for opt_nc, _ in num_cores_tuple_map.values())
    if len(neuron_nodes) > 2:
        # if there are many NeuronOp's in the graph, then don't do any duplication
        max_num_duplicates = 1
    elif len(neuron_nodes) == 2:
        # if there are precisely two NeuronOp's, then creates at most two duplications
        max_num_duplicates = min(2, max_num_duplicates)
    for node in neuron_nodes:
        if node.name in num_cores_tuple_map:
            this_opt_num_cores, _ = num_cores_tuple_map[node.name]
        else:
            this_opt_num_cores = -1
        # Minimum timeout is 10 sec
        # For big models, we arbitrarily allocate 10 sec quota per 1 GB model size.
        est_timeout = len(node.attr['executable'].s) / 1e8
        timeout = max(est_timeout, 10)
        # if this_opt_num_cores is smaller than actual num_cores in runtime, will enforce ninfer==1
        model_config = [global_opt_num_cores, this_opt_num_cores, max_num_duplicates, timeout]
        node.attr['model_config'].list.i[:] = model_config
    return compiled_graph_def
