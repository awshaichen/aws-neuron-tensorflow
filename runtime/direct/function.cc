/* Copyright Amazon Web Services and its Affiliates. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "function.h"
#include <cstddef>
#include <string>
#include <vector>
#include "../tensor_util.h"
#include "absl/memory/memory.h"
#include "adaptor.h"
#include "core_range.h"
#include "executable.h"
#include "executable_info.h"
#include "host_memory.h"
#include "placer.h"
#include "tensorflow/core/framework/node_def.pb.h"
#include "tensorflow/core/framework/op_kernel.h"
#include "tensorflow/core/framework/tensor.h"
#include "tensorflow/core/framework/tensor.pb.h"
#include "tensorflow/core/framework/tensor_shape.h"
#include "tensorflow/core/framework/types.h"
#include "tensorflow/core/lib/core/errors.h"
#include "tensorflow/core/lib/core/status.h"
#include "tensorflow/core/platform/default/logging.h"
#include "tensorflow/core/platform/mutex.h"

namespace tensorflow {
namespace neuron {

NeuronFunction::NeuronFunction() : exe_(nullptr) {}

Status NeuronFunction::Run(OpKernelContext* ctx, const NodeDef& node_def) {
  TF_RETURN_IF_ERROR(MaybeInit(node_def, ctx->session_handle()));
  std::vector<Tensor> inputs;
  TF_RETURN_IF_ERROR(SetupInputs(ctx, node_def, &inputs));
  std::vector<Tensor> outputs;
  TF_RETURN_IF_ERROR(SetupOutputs(ctx, node_def, &outputs));
  TF_RETURN_IF_ERROR(MaybeShuffle(ctx, &inputs));
  NeuronHostMemory memory;
  TF_RETURN_IF_ERROR(memory.SetupBuffers(info_));
  TF_RETURN_IF_ERROR(memory.CopyCPUToInputBuffers(inputs));
  TF_RETURN_IF_ERROR(exe_->RunOnHostMemory(&memory));
  TF_RETURN_IF_ERROR(memory.CopyOutputBuffersToCPU(outputs));
  return Status::OK();
}

Status NeuronFunction::MaybeInit(const NodeDef& node_def,
                                 const std::string& session_handle) {
  tensorflow::mutex_lock lock(mu_);
  if (TF_PREDICT_TRUE(exe_ != nullptr)) {
    VLOG(1) << "NeuronFunction is already initialized";
    return Status::OK();
  }
  NeuronCorePlacer& placer = NeuronCorePlacer::Singleton();
  TF_RETURN_IF_ERROR(placer.GetStatus());
  exe_ = absl::make_unique<NeuronDataParallelExecutable>();
  TF_RETURN_IF_ERROR(info_.ParseFromNodeDef(node_def));
  std::pair<Status, std::vector<NeuronCoreRange>> status_core_ranges =
      placer.GetParallelCoreRanges(info_, session_handle);
  TF_RETURN_IF_ERROR(status_core_ranges.first);
  for (const auto& nc_range : status_core_ranges.second) {
    exe_->AddExecutable(info_.executable, nc_range);
  }
  VLOG(1) << "NeuronFunction::MaybeInit done";
  return Status::OK();
}

Status NeuronFunction::SetupInputs(OpKernelContext* ctx,
                                   const NodeDef& node_def,
                                   std::vector<Tensor>* inputs) {
  int expected_num_inputs = info_.input_dtypes.type_size();
  int num_inputs = ctx->num_inputs();
  if (TF_PREDICT_FALSE(num_inputs != expected_num_inputs)) {
    return errors::InvalidArgument("Invalid number of inputs ", num_inputs,
                                   " on NodeDef \"", node_def.name(),
                                   "\" (expect ", expected_num_inputs, ").");
  }
  // TODO: remove once dynamic batch size is implemented
  for (int idx = 0; idx < num_inputs; ++idx) {
    TensorShape expected_shape(info_.input_shapes.shape(idx));
    if (TF_PREDICT_FALSE(ctx->input(idx).shape() != expected_shape)) {
      return errors::Unimplemented("Dynamic batch size unimplemented");
    }
  }

  // Set inputs
  inputs->reserve(num_inputs);
  for (int idx = 0; idx < num_inputs; ++idx) {
    inputs->push_back(ctx->input(idx));
  }
  for (const Tensor& tensor : *inputs) {
    if (TF_PREDICT_FALSE(!DataTypeCanUseMemcpy(tensor.dtype()))) {
      return errors::InvalidArgument("Input tensor ",
                                     tensor.DeviceSafeDebugString(),
                                     " has unsupported data type");
    }
  }
  VLOG(1) << "NeuronFunction::SetupInputs done";
  return Status::OK();
}

Status NeuronFunction::SetupOutputs(OpKernelContext* ctx,
                                    const NodeDef& node_def,
                                    std::vector<Tensor>* outputs) {
  int expected_num_outputs = info_.output_dtypes.type_size();
  int num_outputs = ctx->num_outputs();
  if (TF_PREDICT_FALSE(num_outputs != expected_num_outputs)) {
    return errors::InvalidArgument("Invalid number of outputs ", num_outputs,
                                   " on NodeDef \"", node_def.name(),
                                   "\" (expect ", expected_num_outputs, ").");
  }

  // Allocate and set outputs
  std::vector<Tensor*> output_tensor_ptrs(num_outputs);
  for (int idx = 0; idx < num_outputs; ++idx) {
    TensorShape shape(info_.output_shapes.shape(idx));
    Tensor** output_ptr_ptr = &output_tensor_ptrs.at(idx);
    TF_RETURN_IF_ERROR(ctx->allocate_output(idx, shape, output_ptr_ptr));
  }
  outputs->reserve(num_outputs);
  for (Tensor* ptr : output_tensor_ptrs) {
    outputs->push_back(*ptr);
  }
  for (const Tensor& tensor : *outputs) {
    if (TF_PREDICT_FALSE(!DataTypeCanUseMemcpy(tensor.dtype()))) {
      return errors::InvalidArgument("Output tensor ",
                                     tensor.DeviceSafeDebugString(),
                                     " has unsupported data type");
    }
  }
  VLOG(1) << "NeuronFunction::SetupOutputs done";
  return Status::OK();
}

Status NeuronFunction::MaybeShuffle(OpKernelContext* ctx,
                                    std::vector<Tensor>* inputs) {
  if (nullptr == info_.input_shuffles) {
    return Status::OK();
  }
  for (int idx = 0; idx < info_.input_shuffles->tensor_size(); ++idx) {
    const TensorProto& shuffle = info_.input_shuffles->tensor(idx);
    if (TF_PREDICT_FALSE(!shuffle.int64_val_size())) {
      continue;
    }
    Tensor src = inputs->at(idx);
    Tensor* dst = &inputs->at(idx);
    TF_RETURN_IF_ERROR(ctx->allocate_temp(src.dtype(), src.shape(), dst));
    TF_RETURN_IF_ERROR(tensor_shuffle(dst, src, shuffle));
  }
  return Status::OK();
}

}  // namespace neuron
}  // namespace tensorflow
