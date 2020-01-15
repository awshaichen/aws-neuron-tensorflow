/* Copyright 2019, Amazon.com, Inc. or its affiliates. All Rights Reserved. */

#include <grpcpp/grpcpp.h>
#include "nmgr.pb.h"
#include "nerr.pb.h"
#include "runtime_grpc.h"


namespace tensorflow {
namespace neuron {


Status RuntimeGRPC::initialize(const std::string &nrtd_address) {
    nrtd_address_ = nrtd_address;
    grpc::ChannelArguments ch_args;
    ch_args.SetMaxReceiveMessageSize(-1);
    ch_args.SetMaxSendMessageSize(-1);
    std::shared_ptr<grpc::Channel> channel = grpc::CreateCustomChannel(
        nrtd_address, grpc::InsecureChannelCredentials(), ch_args);
    if (!channel) {
        return errors::Unavailable(
            "cannot establish grpc channel to neuron-rtd server");
    }
    stub_ = nrt::nmgr_v1::NewStub(channel);
    if (!stub_) {
        return errors::Unavailable("cannot create stub");
    }
    return Status::OK();
}

Status RuntimeGRPC::create_eg(uint32_t *eg_id, uint32_t *num_cores,
                              const int num_cores_req) {
    nrt::create_eg_request request;
    if (num_cores_req >= 0) {
        request.set_nc_count((uint32_t)num_cores_req);
    }
    nrt::create_eg_response response;
    grpc::Status status = NRT_GRPC(stub_->create_eg, request, &response);
    if (!status.ok() && grpc::StatusCode::UNAVAILABLE == status.error_code()) {
        std::string message(" is unavailable. Is neuron-rtd running?");
        std::string unix_prefix("unix:");
        size_t start = nrtd_address_.find(unix_prefix);
        if (0 == start) {
            message += " Is socket ";
            message += nrtd_address_.substr(start + unix_prefix.length());
            message += " writable?";
        }
        return errors::Unavailable("grpc server ", nrtd_address_, message);
    }
    NRT_CHECK_RETURN("create_eg", status, response);
    *eg_id = response.h_eg().id();
    *num_cores = response.nc_count();
    return Status::OK();
}

Status RuntimeGRPC::load(uint32_t *nn_id, const uint32_t eg_id,
                         const StringPiece &executable,
                         const uint32_t timeout, const uint32_t ninfer) {
    // load
    grpc::ClientContext context;
    nrt::load_response response;
    std::unique_ptr<grpc::ClientWriter<nrt::load_request> > writer(
        stub_->load(&context, &response));
    nrt::load_request request;

    #define WRITE_LOAD_REQUEST {                                                \
        if (!writer->Write(request)) {                                          \
            return errors::Internal("neuron-rtd load failure - broken stream"); \
        }                                                                       \
    }
    // eg_id
    request.mutable_h_eg()->set_id(eg_id);
    WRITE_LOAD_REQUEST;

    // neff_size
    size_t exec_total_size = executable.size();
    request.set_neff_size(exec_total_size);
    WRITE_LOAD_REQUEST;

    // model_params
    nrt::model_params *model_params = request.mutable_model_params();
    model_params->mutable_timeout()->set_data(timeout);
    model_params->mutable_ninfer()->set_data(ninfer);
    WRITE_LOAD_REQUEST;

    // neff file content
    for (size_t pos = 0; pos < exec_total_size; pos += EXEC_MAX_CHUNK_SIZE) {
        size_t remaining = exec_total_size - pos;
        size_t chunk_size = std::min(remaining, EXEC_MAX_CHUNK_SIZE);
        StringPiece file_chunk = executable.substr(pos, chunk_size);
        request.mutable_neff_chunk()->set_chunk(file_chunk.data(), chunk_size);
        WRITE_LOAD_REQUEST;
    }
    if (!writer->WritesDone()) {
        return errors::Internal("neuron-rtd load failure - broken stream");
    }
    grpc::Status status = writer->Finish();
    NRT_CHECK_RETURN("load", status, response);
    *nn_id = response.h_nn().id();
    return Status::OK();
}

Status RuntimeGRPC::start(const uint32_t nn_id) {
    nrt::start_request request;
    request.mutable_h_nn()->set_id(nn_id);
    nrt::start_response response;
    grpc::Status status = NRT_GRPC(stub_->start, request, &response);
    NRT_CHECK_RETURN("start", status, response);
    return Status::OK();
}

Status RuntimeGRPC::infer(std::vector<Tensor*> *output_tensors, Timestamps *timestamps,
                          const uint32_t nn_id,
                          AttrList &input_names, AttrList &output_names,
                          const std::vector<const Tensor*> &input_tensors,
                          const SharedMemory &shm) {
    nrt::infer_request request;
    for (int idx = 0; idx < input_names.s_size(); ++idx) {
        nrt::infer_io *infer_io = request.add_ifmap();
        infer_io->set_name(input_names.s(idx));
        StringPiece tensor_data(input_tensors[idx]->tensor_data());
        if (shm.enabled_) {
            infer_io->mutable_buf_shm()->set_path(shm.input_paths_[idx]);
            std::memcpy(shm.input_ptrs_[idx], tensor_data.data(), tensor_data.size());
        } else {
            infer_io->set_buf(tensor_data.data(), tensor_data.size());
        }
    }
    if (shm.enabled_) {
        for (int idx = 0; idx < output_names.s_size(); ++idx) {
            nrt::infer_io *infer_io = request.add_shm_ofmap();
            infer_io->set_name(output_names.s(idx));
            infer_io->mutable_buf_shm()->set_path(shm.output_paths_[idx]);
        }
    }
    request.mutable_h_nn()->set_id(nn_id);
    nrt::infer_response response;

    // infer
    if (nullptr != timestamps) timestamps->mark_above_nrtd_infer();
    grpc::Status status = NRT_GRPC(stub_->infer, request, &response);
    if (nullptr != timestamps) timestamps->mark_below_nrtd_infer();
    if (status.ok()) {
        // ignore inf/nan errors
        if (nrt::nerr::NERR_INFER_COMPLETED_WITH_NUM_ERR == response.status().code()) {
            response.mutable_status()->set_code(nrt::nerr::NERR_OK);
        }
    }
    NRT_CHECK_RETURN("infer", status, response);
    if (shm.enabled_) {
        for (int idx = 0; idx < output_names.s_size(); ++idx) {
            nrt::infer_io *infer_io = response.add_ofmap();
            infer_io->set_name(output_names.s(idx));
            infer_io->set_buf(shm.output_ptrs_[idx], shm.output_sizes_[idx]);
        }
    }
    if (nullptr != output_tensors) {
        TF_RETURN_IF_ERROR(copy_output_tensors(output_tensors, response, output_names));
    }
    return Status::OK();
}

Status RuntimeGRPC::infer_post(NMGROutputs *nmgr_outputs, Timestamps *timestamps,
                               const uint32_t nn_id, AttrList &input_names,
                               const std::vector<const Tensor*> &input_tensors) {
    nrt::infer_request request;
    for (auto idx = 0; idx < input_names.s_size(); ++idx) {
        nrt::infer_io *infer_io = request.add_ifmap();
        infer_io->set_name(input_names.s(idx));
        StringPiece tensor_data(input_tensors[idx]->tensor_data());
        infer_io->set_buf((void*)tensor_data.data(), tensor_data.size());
    }
    request.mutable_h_nn()->set_id(nn_id);

    // infer
    nrt::infer_post_response response;
    if (nullptr != timestamps) timestamps->mark_above_nrtd_infer();
    grpc::Status status = NRT_GRPC(stub_->infer_post, request, &response);
    NRT_CHECK_RETURN("infer_post", status, response);
    nmgr_outputs->cookie = response.cookie();
    return Status::OK();
}

Status RuntimeGRPC::infer_wait(std::vector<Tensor*> *output_tensors,
                               Timestamps *timestamps,
                               const NMGROutputs &nmgr_outputs, AttrList &output_names) {
    nrt::infer_wait_request request;
    nrt::infer_response response;
    request.set_cookie(nmgr_outputs.cookie);

    // infer_wait
    grpc::Status status = NRT_GRPC(stub_->infer_wait, request, &response);
    if (nullptr != timestamps) timestamps->mark_below_nrtd_infer();
    if (status.ok()) {
        // ignore inf/nan errors
        if (nrt::nerr::NERR_INFER_COMPLETED_WITH_NUM_ERR == response.status().code()) {
            response.mutable_status()->set_code(nrt::nerr::NERR_OK);
        }
    }
    NRT_CHECK_RETURN("infer_wait", status, response);
    TF_RETURN_IF_ERROR(copy_output_tensors(output_tensors, response, output_names));
    return Status::OK();
}

Status RuntimeGRPC::stop(const uint32_t nn_id) {
    nrt::stop_request request;
    request.mutable_h_nn()->set_id(nn_id);
    nrt::stop_response response;
    grpc::Status status = NRT_GRPC(stub_->stop, request, &response);
    NRT_CHECK_RETURN("stop", status, response);
    return Status::OK();
}

Status RuntimeGRPC::unload(const uint32_t nn_id) {
    nrt::unload_request request;
    request.mutable_h_nn()->set_id(nn_id);
    nrt::unload_response response;
    grpc::Status status = NRT_GRPC(stub_->unload, request, &response);
    NRT_CHECK_RETURN("unload", status, response);
    return Status::OK();
}

Status RuntimeGRPC::destroy_eg(const uint32_t eg_id) {
    nrt::destroy_eg_request request;
    request.mutable_h_eg()->set_id(eg_id);
    nrt::destroy_eg_response response;
    grpc::Status status = NRT_GRPC(stub_->destroy_eg, request, &response);
    NRT_CHECK_RETURN("destroy_eg", status, response);
    return Status::OK();
}

Status RuntimeGRPC::shm_map(const std::string &path, const uint32_t mmap_prot) {
    nrt::shm_map_request request;
    request.set_path(path);
    request.set_mmap_prot(mmap_prot);
    nrt::shm_map_response response;
    grpc::Status status = NRT_GRPC(stub_->shm_map, request, &response);
    NRT_CHECK_RETURN("shm_map", status, response);
    return Status::OK();
}

Status RuntimeGRPC::shm_unmap(const std::string &path, const uint32_t mmap_prot) {
    nrt::shm_unmap_request request;
    request.set_path(path);
    request.set_mmap_prot(mmap_prot);
    nrt::shm_unmap_response response;
    grpc::Status status = NRT_GRPC(stub_->shm_unmap, request, &response);
    NRT_CHECK_RETURN("shm_unmap", status, response);
    return Status::OK();
}


Status copy_output_tensors(std::vector<Tensor*> *output_tensors,
                           const nrt::infer_response &response,
                           AttrList &output_names) {
    // set output tensors
    std::vector<StringPiece> raw_output_tensors;
    std::unordered_map<std::string, StringPiece> map_name_raw;
    for (const auto &infer_io : response.ofmap()) {
        map_name_raw.emplace(infer_io.name(), infer_io.buf());
    }
    for (auto idx = 0; idx < output_names.s_size(); ++idx) {
        if (map_name_raw.find(output_names.s(idx)) == map_name_raw.end()) {
            return errors::Internal("tensor name", output_names.s(idx),
                                    " not found in infer_response.ofmap()");
        }
        raw_output_tensors.push_back(map_name_raw[output_names.s(idx)]);
    }
    for (auto idx = 0; idx < output_names.s_size(); ++idx) {
        StringPiece out_tensor_raw = raw_output_tensors[idx];
        Tensor *out_tensor = output_tensors->at(idx);
        TF_RETURN_WITH_CONTEXT_IF_ERROR(tensor_memcpy(out_tensor, out_tensor_raw),
                                        "tensor_memcpy failure on tensor name: ",
                                        output_names.s(idx));
    }
    return Status::OK();
}


}  // namespace neuron
}  // namespace tensorflow
