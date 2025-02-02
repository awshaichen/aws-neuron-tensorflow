# Copyright Amazon Web Services and its Affiliates. All Rights Reserved.
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
import math
import reprlib
from collections import namedtuple
from tensorflow.core.framework import graph_pb2
from tensorflow.core.framework import types_pb2
from tensorflow.python.framework import dtypes
from tensorflow.python.framework.ops import convert_to_tensor
from tensorflow.python.framework.tensor_shape import TensorShape
from tensorflow.python.platform import tf_logging as logging
from tensorflow_neuron.python.neuron_cc import compile_savetemps
from tensorflow_neuron.python import neff_util
from tensorflow_neuron.python import utils


tNeuronOp = 'NeuronOp'
tPlaceholder = 'Placeholder'
kNeuronInferredShapes = '_aws_neuron_inferred_shapes'
kOutputShapes = '_output_shapes'
knExecutable = 'executable'
knGraphDef = 'graph_def'
knInputNames = 'input_names'
knOutputNames = 'output_names'
knInputDtypes = 'input_dtypes'
knOutputDtypes = 'output_dtypes'
knInputShapes = 'input_shapes'
knOutputShapes = 'output_shapes'
knInputBatchAxis = 'input_batch_axis'
knInputShuffles = '_input_shuffles'
knInputCanUseShm = '_input_can_use_shm'
knRealInputNames = '_real_input_names'
knRealInputLocations = '_real_input_locations'
vInvalidAxis = -1


def normalize_operators(graph_def):
    gd_tensor_name_to_consumers = {}
    gd_tensor_name_to_shape = {}
    for node in graph_def.node:
        for inp in node.input:
            if inp not in gd_tensor_name_to_consumers:
                gd_tensor_name_to_consumers[inp] = []
            gd_tensor_name_to_consumers[inp].append(inp)
        if kOutputShapes in node.attr:
            for idx, shape in enumerate(node.attr[kOutputShapes].list.shape):
                tensor_name = node.name if idx == 0 else '{}:{}'.format(node.name, idx)
                gd_tensor_name_to_shape[tensor_name] = shape
    for node in graph_def.node:
        if node.op == 'StopGradient':
            node.op = 'Identity'
        elif node.op == 'FusedBatchNormV3':  # can be replace by FusedBatchNorm for inference
            if node.attr['T'].type != dtypes.float32.as_datatype_enum:
                continue
            found_training_consumer = False
            for idx in range(3, 6):
                gd_tensor_name = '{}:{}'.format(node.name, idx)
                if gd_tensor_name_to_consumers.get(gd_tensor_name, False):
                    found_training_consumer = True
            if not found_training_consumer:
                node.op = 'FusedBatchNorm'
                node.attr.pop('U')
                if kOutputShapes in node.attr:
                    node.attr[kOutputShapes].list.shape.pop()
        elif node.op == 'AddV2':
            node.op = 'Add'
        elif node.op == 'BatchMatMulV2':  # only change to BatchMatMul if no broadcast
            input0, input1 = node.input[0], node.input[1]
            if input0 not in gd_tensor_name_to_shape:
                continue
            if input1 not in gd_tensor_name_to_shape:
                continue
            shape0 = TensorShape(gd_tensor_name_to_shape[input0])
            shape1 = TensorShape(gd_tensor_name_to_shape[input1])
            if shape0.rank is not None and shape0.rank == shape1.rank and shape0[:-2] == shape1[:-2]:
                node.op = 'BatchMatMul'
    return graph_def


def encode_inferred_shapes(graph_def, shape_feed_dict=None):
    if shape_feed_dict is not None:
        name_to_ports = {}
        for tensor_name in shape_feed_dict.keys():
            node_name, port = tensor_name.split(':')
            port = int(port)
            if node_name not in name_to_ports:
                name_to_ports[node_name] = set()
            name_to_ports[node_name].add(port)
        for node in graph_def.node:
            if node.name in name_to_ports:
                inferred_shapes = node.attr[kNeuronInferredShapes].list
                port_set = name_to_ports[node.name]
                max_port = max(port_set)
                for port in range(max_port + 1):
                    shape = inferred_shapes.shape.add()
                    if port in port_set:
                        for size in shape_feed_dict['{}:{}'.format(node.name, port)]:
                            shape.dim.add().size = size
    for node in graph_def.node:
        if kOutputShapes in node.attr:
            output_shapes = node.attr[kOutputShapes]
            if all(TensorShape(shape).is_fully_defined() for shape in output_shapes.list.shape):
                node.attr[kNeuronInferredShapes].CopyFrom(output_shapes)
    return graph_def


def encode_real_input_names_and_locations(graph_def):
    neuron_nodes = get_neuron_nodes(graph_def)
    for node in neuron_nodes:
        real_input_names_list = []
        real_input_locations_list = []
        for i in range(len(node.input)):
            if 'ReadVariableOp' not in node.input[i]:
                real_input_names_list.append(node.input[i].encode())
                real_input_locations_list.append(i)
        node.attr[knRealInputNames].list.s[:] = real_input_names_list
        node.attr[knRealInputLocations].list.i[:] = real_input_locations_list
    return graph_def
  		  

def shape_inference_with_inputs(graph_def, sess, feed_dict):
    """Infer tensor shapes by running inference.

    Args:
        graph_def: A tensorflow `GraphDef` protobuf message.
        sess: An active tensorflow Session where we can perform inference to find tensor shapes
        feed_dict: dict `{str: numpy.ndarray}` that maps tensor names to input
            numpy arrays, as used in a full inference with session.run.

    Returns:
        A new tensorflow `GraphDef` protobuf message that possibly has NeuronOp's attributes
        `input_shapes` and `output_shapes` filled.
    """
    neuron_nodes = get_neuron_nodes(graph_def)
    tensor_name_map = {}
    for node in neuron_nodes:
        for port, name in enumerate(node.attr[knOutputNames].list.s):
            tensor_name_map['{}:{}'.format(node.name, port)] = name.decode()
    need_shape = []
    for node in neuron_nodes:
        input_shapes = node.attr[knInputShapes].list.shape
        output_names = node.attr[knOutputNames].list.s
        output_shapes = node.attr[knOutputShapes].list.shape
        for name, shape_proto in zip(node.input, input_shapes):
            if not TensorShape(shape_proto).is_fully_defined():
                if ':' not in name:
                    name = '{}:0'.format(name)
                need_shape.append(tensor_name_map.get(name, name))
        for name, shape_proto in zip(output_names, output_shapes):
            if not TensorShape(shape_proto).is_fully_defined():
                need_shape.append(name.decode())
    need_shape = [sess.graph.get_tensor_by_name(name) for name in need_shape]
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
        need_shape_infer_np = sess.run(need_shape_infer, feed_dict)
        inferred_shapes = {ts.name: TensorShape(ts_np.shape) for ts, ts_np in zip(need_shape_infer, need_shape_infer_np)}
        for node in neuron_nodes:
            input_shapes = node.attr[knInputShapes].list.shape
            output_names = node.attr[knOutputNames].list.s
            output_shapes = node.attr[knOutputShapes].list.shape
            for idx, (name, shape_proto) in enumerate(zip(node.input, input_shapes)):
                if not TensorShape(shape_proto).is_fully_defined():
                    if ':' not in name:
                        name = '{}:0'.format(name)
                    name = tensor_name_map.get(name, name)
                    input_shapes[idx].CopyFrom(inferred_shapes[name].as_proto())
            for idx, (name, shape_proto) in enumerate(zip(output_names, output_shapes)):
                if not TensorShape(shape_proto).is_fully_defined():
                    output_shapes[idx].CopyFrom(inferred_shapes[name.decode()].as_proto())
    return graph_def


def inline_shape_inputs_in_subgraphs(graph_def):
    """
    If NeuronOp has inputs that come from shape-related operators, then inline
    them as constants in the subgraph and remove them from the input signature.

    Note that the current approach only deals with inputs that come from
    shape-related operators directly. In theory, a safer approach is to copy
    the entire graph, turn all inferrable shapes into constants, and then
    propagate through constant folding. It is not practical at this point as
    copying the entire graph would consume too much memory and it will become
    practical once we don't have to freeze the entire graph.
    """
    name_to_node = {node.name: node for node in graph_def.node}
    shape_content_fn_map = {'Shape': TensorShape.as_list, 'Size': TensorShape.num_elements}

    def get_node(name):
        node_name, _ = split_tensor_name(name)
        return name_to_node[node_name]

    def contains_shape_input(node):
        # Returns True if any non-control input node is a shape-related operator
        return any(get_node(name).op in shape_content_fn_map for name in node.input if not name.startswith('^'))

    if not any(contains_shape_input(node) for node in get_neuron_nodes(graph_def)):
        return graph_def
    for node in get_neuron_nodes(graph_def):
        subgraph_def = get_subgraph_def(node)
        subgraph_name_to_node = {sn.name: sn for sn in subgraph_def.node}
        attr = node.attr
        discards = set()
        for idx, (input_name, ph_name) in enumerate(zip(node.input, attr[knInputNames].list.s)):
            input_node = get_node(input_name)
            if input_node.op in shape_content_fn_map:
                shape_input_name, = input_node.input
                shape_input_node_name, port = split_tensor_name(shape_input_name)
                shape_input_node = name_to_node[shape_input_node_name]
                shape_attr = shape_input_node.attr.get(kNeuronInferredShapes, None)
                if shape_attr is None:
                    shape_attr = shape_input_node.attr.get(knOutputShapes, None)
                if shape_attr is None:
                    continue
                shape_proto = shape_attr.list.shape[port]
                shape = TensorShape(shape_proto)
                dtype_enum = input_node.attr['out_type'].type
                dtype = dtypes.as_dtype(dtype_enum)
                tensor_content = shape_content_fn_map[input_node.op](shape)
                shape_tensor = convert_to_tensor(tensor_content, dtype)
                ph_node_name, _ = split_tensor_name(ph_name.decode())
                ph_node = subgraph_name_to_node[ph_node_name]
                ph_node.attr['dtype'].type = dtype_enum
                ph_node.attr.pop('shape')
                tensor_proto = ph_node.attr['value'].tensor
                tensor_proto.dtype = dtype_enum
                tensor_proto.tensor_shape.CopyFrom(shape_tensor.shape.as_proto())
                tensor_proto.tensor_content = shape_tensor.numpy().tobytes()
                ph_node.op = 'Const'
                discards.add(idx)
        if not discards:
            continue

        def maybe_discard_from_scalar_container(container):
            if container:
                container[:] = [value for idx, value in enumerate(container) if idx not in discards]

        def maybe_discard_from_composite_container(container):
            if container:
                new_values = [value for idx, value in enumerate(container) if idx not in discards]
                while container:
                    container.pop()
                for value in new_values:
                    container.add().CopyFrom(value)

        scalar_containers = [
            node.input,
            attr[knInputNames].list.s,
            attr[knInputDtypes].list.type,
            attr[knInputBatchAxis].list.i,
        ]
        for container in scalar_containers:
            maybe_discard_from_scalar_container(container)
        maybe_discard_from_composite_container(attr[knInputShapes].list.shape)
        if knInputShuffles in attr:
            maybe_discard_from_composite_container(attr[knInputShuffles].list.tensor)
        if knInputCanUseShm in attr:
            maybe_discard_from_scalar_container(attr[knInputCanUseShm].list.b)
        node.attr[knGraphDef].s = subgraph_def.SerializeToString()
    return graph_def


def convert_shape_to_constant(graph_def):
    name_to_node = {node.name: node for node in graph_def.node}
    for node in graph_def.node:
        if node.op in {'Shape', 'Size'}:
            input_node_name, port = split_tensor_name(node.input[0])
            input_node = name_to_node[input_node_name]
            shape_proto = input_node.attr[kNeuronInferredShapes].list.shape[port]
            shape = TensorShape(shape_proto)
            if shape.is_fully_defined():
                node.input[:] = []
                dtype_enum = node.attr['out_type'].type
                node.attr['dtype'].type = dtype_enum
                node.attr.pop('T')
                node.attr.pop('out_type')
                dtype = dtypes.as_dtype(dtype_enum)
                tensor_proto = node.attr['value'].tensor
                tensor_proto.dtype = dtype_enum
                if node.op == 'Shape':
                    tensor_content = shape.as_list()
                elif node.op == 'Size':
                    tensor_content = shape.num_elements()
                shape_tensor = convert_to_tensor(tensor_content, dtype)
                tensor_proto.tensor_shape.CopyFrom(shape_tensor.shape.as_proto())
                tensor_proto.tensor_content = shape_tensor.numpy().tobytes()
                node.op = 'Const'
    return graph_def


def run_graph_def_pass_in_subgraphs(graph_def, graph_def_pass):
    for node in get_neuron_nodes(graph_def):
        subgraph_def = get_subgraph_def(node)
        subgraph_def = graph_def_pass(subgraph_def)
        node.attr[knGraphDef].s = subgraph_def.SerializeToString()
    return graph_def


def run_compiler_on_subgraphs(graph_def, dumper):
    IOTensor = namedtuple('IOTensor', 'name, dtype, shape')
    for node in get_neuron_nodes(graph_def):
        is_compilable, reason = neuron_node_is_compilable(node)
        if not is_compilable:
            logging.warning('Not fusing subgraph {} because {}'.format(node.name, reason))
            continue

        # get graph_def and io tensors
        subgraph_def = get_subgraph_def(node)
        inputs = []
        outputs = []
        nal = lambda key: node.attr[key].list
        zip_inputs = zip(nal(knInputNames).s, nal(knInputDtypes).type, nal(knInputShapes).shape)
        zip_outputs = zip(nal(knOutputNames).s, nal(knOutputDtypes).type, nal(knOutputShapes).shape)
        for container, tensors in zip([inputs, outputs], [zip_inputs, zip_outputs]):
            for name, dtype_enum, shape in tensors:
                name = name.decode()
                dtype = dtypes.as_dtype(dtype_enum)
                tensor = IOTensor(name, dtype, shape)
                container.append(tensor)

        # remove attributes that are not recognized by neuron-cc
        for sg_node in subgraph_def.node:
            sg_node.attr.pop(kNeuronInferredShapes)

        # setup workdir and run neuron-cc
        executable, inputs, outputs = compile_savetemps(subgraph_def, inputs, outputs, node.name, dumper)
        if executable:
            node.attr[knExecutable].s = executable
            node.attr[knInputNames].list.s[:] = [ts.name.encode() for ts in inputs]
            node.attr[knOutputNames].list.s[:] = [ts.name.encode() for ts in outputs]
            try:
                input_shuffles = [inp.shuffle for inp in inputs]
            except AttributeError:
                input_shuffles = [None for inp in inputs]
            do_input_shuffles = any(shuffle is not None for shuffle in input_shuffles)
            if do_input_shuffles:
                for shuffle in input_shuffles:
                    idx_ts = node.attr['_input_shuffles'].list.tensor.add()
                    idx_ts.dtype = types_pb2.DataType.DT_INT64
                    idx_ts.tensor_shape.dim.add().size = len(shuffle)
                    idx_ts.int64_val.extend(shuffle)
            try:
                input_batch_axis = [ts.batch_axis for ts in inputs]
                output_batch_axis = [ts.batch_axis for ts in outputs]
            except AttributeError:
                pass
            else:
                input_batch_axis = [vInvalidAxis if ax is None else ax for ax in input_batch_axis]
                output_batch_axis = [vInvalidAxis if ax is None else ax for ax in output_batch_axis]
                node.attr['input_batch_axis'].list.i[:] = input_batch_axis
                node.attr['output_batch_axis'].list.i[:] = output_batch_axis
                input_args = node.attr[knInputShapes].list.shape, inputs
                output_args = node.attr[knOutputShapes].list.shape, outputs
                for args in input_args, output_args:
                    for shape, ts in zip(*args):
                        if [dim.size for dim in shape.dim] != ts.shape:
                            for dim, size in zip(shape.dim, ts.shape):
                                dim.size = size
            try:
                input_can_use_shm = [ts.can_use_shm for ts in inputs]
                output_can_use_shm = [ts.can_use_shm for ts in outputs]
            except AttributeError:
                pass
            else:
                node.attr['_input_can_use_shm'].list.b[:] = input_can_use_shm
                node.attr['_output_can_use_shm'].list.b[:] = output_can_use_shm
    return graph_def


def neuron_node_is_compilable(node):
    reasons = []
    # skip compiling this subgraph for the following reasons
    if len(node.attr[knInputNames].list.s) == 0:
        reasons.append('it does not have inputs')
    if len(node.attr[knOutputNames].list.s) == 0:
        reasons.append('it does not have outputs')
    if any(not TensorShape(shape).is_fully_defined() for shape in node.attr[knInputShapes].list.shape):
        reasons.append('input shapes are not fully defined')
    if any(not TensorShape(shape).is_fully_defined() for shape in node.attr[knOutputShapes].list.shape):
        reasons.append('output shapes are not fully defined')
    if reasons:
        return False, ' and '.join(reasons)
    else:
        return True, None


def restore_compiler_failures(compiled_graph_def, original_graph_def):
    """
    Restore `NeuronOp`'s that failed to compile.

    TODO: Some passes introduced recently can change subgraph input/output
    signatures. To deal with these cases properly, we need to obtain original
    NodeDef messages from `original_graph_def` instead of `subgraph_def`.
    """
    neuron_op_dict = {node.name: node for node in get_neuron_nodes(compiled_graph_def)}
    restore_nodes = []
    remove_node_names = set()
    gd_tensor_name_map = {}
    all_expected_node_names = {node.name for node in compiled_graph_def.node if node.op != tNeuronOp}
    for node in get_neuron_nodes(compiled_graph_def):
        if not node.attr[knExecutable].s:
            remove_node_names.add(node.name)
            subgraph_def = get_subgraph_def(node)
            sgd_tensor_name_map = {}
            for gd_ts_name, sg_ph_name in zip(node.input, node.attr[knInputNames].list.s):
                sgd_ph_name = format_tensor_name(sg_ph_name.decode())
                op_name, ts_index = _graph_def_op_index(gd_ts_name)
                if op_name in neuron_op_dict:
                    in_node = neuron_op_dict[op_name]
                    if not in_node.attr[knExecutable].s:
                        gd_ts_name = in_node.attr[knOutputNames].list.s[ts_index].decode()
                sgd_tensor_name_map[sgd_ph_name] = gd_ts_name
            for sg_node in subgraph_def.node:
                for idx, name in enumerate(sg_node.input):
                    sg_node.input[idx] = sgd_tensor_name_map.get(name, name)
                if sg_node.op != tPlaceholder:
                    restore_nodes.append(sg_node)
                    all_expected_node_names.add(sg_node.name)
            for out_idx, out_name in enumerate(node.attr[knOutputNames].list.s):
                out_gd_ts_name = format_tensor_name('{}:{}'.format(node.name, out_idx))
                gd_tensor_name_map[out_gd_ts_name] = format_tensor_name(out_name.decode())
    restore_node_names = {node.name for node in restore_nodes}
    remove_node_names.update(
        node.name for node in compiled_graph_def.node if node.name in restore_node_names)
    original_node_with_control_inputs = get_node_with_control_inputs(original_graph_def)
    for node in restore_nodes:
        if node.name in original_node_with_control_inputs:
            input_names = original_node_with_control_inputs[node.name]
            for name in input_names:
                if name.split(':')[0] in all_expected_node_names:
                    node.input.append(name)
    for node in compiled_graph_def.node:
        for idx, name in enumerate(node.input):
            node.input[idx] = gd_tensor_name_map.get(name, name)

    graph_def = graph_pb2.GraphDef()
    graph_def.node.extend(
        node for node in compiled_graph_def.node if node.name not in remove_node_names)
    graph_def.node.extend(node for node in restore_nodes)

    # remove illegal node names
    node_names = {node.name for node in graph_def.node}
    for node in graph_def.node:
        node.input[:] = [name for name in node.input if _graph_def_op_index(name)[0] in node_names]

    # preserve information for function-call operators (e. g., MapDataset)
    graph_def.library.CopyFrom(compiled_graph_def.library)
    return graph_def


def set_execution_plan(compiled_graph_def):
    # scan to get num neuroncores and total number of bytes of input and output tensors
    num_cores_tuple_map = {}
    mis_config = False
    neuron_nodes = list(get_neuron_nodes(compiled_graph_def))
    for node in neuron_nodes:
        num_cores_tuple = neff_util.get_cores_from_executable(node.attr[knExecutable].s)
        if num_cores_tuple is None:
            mis_config = True
        else:
            opt_num_cores, _ = num_cores_tuple
            num_cores_tuple_map[node.name] = num_cores_tuple
    max_num_duplicates = 64
    tfn_args, _ = utils.parse_neuron_cc_flags()
    if mis_config or not num_cores_tuple_map:
        global_opt_num_cores = -1
        max_num_duplicates = 1
    elif tfn_args.extract_weights:
        max_num_duplicates = min(4, calculate_max_num_cores(compiled_graph_def))
        global_opt_num_cores = max(opt_nc for opt_nc, _ in num_cores_tuple_map.values())
    else:
        global_opt_num_cores = max(opt_nc for opt_nc, _ in num_cores_tuple_map.values())
    if len(neuron_nodes) > 1:
        # if there are many NeuronOp's in the graph, then don't do any duplication
        max_num_duplicates = 1
    for node in neuron_nodes:
        if node.name in num_cores_tuple_map:
            this_opt_num_cores, _ = num_cores_tuple_map[node.name]
        else:
            this_opt_num_cores = -1
        # Minimum timeout is 10 sec
        # For big models, we arbitrarily allocate 10 sec quota per 1 GB model size.
        est_timeout = len(node.attr[knExecutable].s) / 1e8
        timeout = int(max(est_timeout, 10))
        # if this_opt_num_cores is smaller than actual num_cores in runtime, will enforce ninfer==1
        model_config = [global_opt_num_cores, this_opt_num_cores, max_num_duplicates, timeout]
        node.attr['model_config'].list.i[:] = model_config
    return compiled_graph_def


def calculate_max_num_cores(compiled_graph_def):
    # returns the amount number of models that can fit on one channel of memory
    # NEFF and Weights get loaded into device memory and
    # One channel of memory is 4gb and there are two channels on an inf1.2xlarge
    # TODO: Support all inf1 instance types
    neuron_nodes = get_neuron_nodes(compiled_graph_def)
    neff_size = 0
    weights_size = 0

    for node in neuron_nodes:
        neff_size += len(node.attr[knExecutable].s)
        for shape, dtype in zip(node.attr['input_shapes'].list.shape, node.attr['input_dtypes'].list.type):
            num_elements = 1 #accumulator var for calcuating number of elements in a given input tensor
            for dim in shape.dim:
                num_elements *= dim.size
            #multiply num of elements * dtype size to get size of tensor
            weights_size += num_elements * dtypes.as_dtype(dtype).size

    return math.floor(4e9 / (neff_size + weights_size)) * 2



def get_neuron_nodes(graph_def):
    return [node for node in graph_def.node if node.op == tNeuronOp]


def get_subgraph_def(node, volatile=False):
    graph_def = graph_pb2.GraphDef()
    graph_def.ParseFromString(node.attr[knGraphDef].s)
    if volatile:
        erase_large_constants(graph_def)
    return graph_def


def erase_large_constants(graph_def):
    # Destroys the input graph_def! Please don't call it on a graph_def that you'll use later
    large_const_threshold = 1024
    for node in graph_def.node:
        if node.op == 'Const' and node.ByteSize() > large_const_threshold:
            tensor = node.attr['value'].tensor
            tensor.tensor_content = b''
            tensor.bool_val[:] = []
            tensor.dcomplex_val[:] = []
            tensor.double_val[:] = []
            tensor.float_val[:] = []
            tensor.half_val[:] = []
            tensor.int64_val[:] = []
            tensor.int_val[:] = []
            tensor.scomplex_val[:] = []
            tensor.string_val[:] = []
            tensor.uint32_val[:] = []
            tensor.uint64_val[:] = []
    return graph_def


def maybe_relax_placeholder_shapes(graph_def):
    need_relaxation = False
    for node in graph_def.node:
        if node.op == tNeuronOp:
            if any(ax != vInvalidAxis for ax in node.attr['input_batch_axis'].list.i):
                need_relaxation = True
    if need_relaxation:
        for node in graph_def.node:
            if node.op == tPlaceholder:
                dims = node.attr['shape'].shape.dim
                if dims:
                    dims[0].size = -1
    return graph_def


def prefix_node_names(graph_def):
    name_change_map = {}
    for node in get_neuron_nodes(graph_def):
        prefix = utils.most_popular_namescope(sn.name for sn in get_subgraph_def(node).node)
        if not prefix:
            continue
        new_op_name = '/'.join([prefix, node.name])
        num_tensor = len(node.attr[knOutputNames].list.s)
        for idx in range(num_tensor):
            tensor_name = format_tensor_name('{}:{}'.format(node.name, idx))
            new_tensor_name = format_tensor_name('{}:{}'.format(new_op_name, idx))
            name_change_map[tensor_name] = new_tensor_name
        node.name = new_op_name
    for node in graph_def.node:
        node.input[:] = [name_change_map.get(inp, inp) for inp in node.input]
    return graph_def


def format_tensor_name(tensor_name):
    return tensor_name.split(':')[0] if tensor_name.endswith(':0') else tensor_name


def split_tensor_name(tensor_name):
    op_name, port = tensor_name.split(':') if ':' in tensor_name else (tensor_name, 0)
    return op_name, int(port)


def get_node_with_control_inputs(graph_def):
    node_with_control_inputs = {}
    for node in graph_def.node:
        control_inputs = [inp for inp in node.input if inp.startswith('^')]
        if control_inputs:
            node_with_control_inputs[node.name] = control_inputs
    return node_with_control_inputs


def compiled_graph_op_counts(graph_def):
    neuron_nodes = [node for node in graph_def.node if node.op == tNeuronOp]
    num_ops_on_neuron = 0
    for node in neuron_nodes:
        if node.attr['executable'].s:
            subgraph_def = get_subgraph_def(node)
            num_ops_on_neuron += len(subgraph_def.node) - len(node.attr['input_names'].list.s)
    num_ops_tfn = len(graph_def.node) + num_ops_on_neuron - len(neuron_nodes)
    return max(num_ops_tfn, 0), max(num_ops_on_neuron, 0)


def _graph_def_op_index(graph_def_tensor_name):
    if ':' in graph_def_tensor_name:
        op_name, value_index = graph_def_tensor_name.split(':')
        value_index = int(value_index)
    else:
        op_name, value_index = graph_def_tensor_name, 0
    if op_name.startswith('^'):
        op_name = op_name[1:]
    return op_name, value_index
