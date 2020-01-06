/* Copyright 2019, Amazon.com, Inc. or its affiliates. All Rights Reserved. */

#include <sys/mman.h>
#include <fcntl.h>
#include <grpcpp/grpcpp.h>
#include "tensorflow/core/platform/env.h"
#include "neuron_clib.h"
#include "nmgr.pb.h"
#include "nerr.pb.h"
#ifdef NEURONTFSERV
#include <csignal>
#endif  // NEURONTFSERV


namespace tensorflow {
namespace neuron {


NeuronDeviceManager global_neuron_device_manager;


#ifdef NEURONTFSERV
void sigint_handler(int sig) {
    global_neuron_device_manager.clear();
    std::signal(SIGINT, SIG_DFL);
    std::signal(SIGTERM, SIG_DFL);
    std::raise(sig);
}
#endif // NEURONTFSERV


static std::string gen_shm_name(uint32_t nn_id) {
    std::string filename = "/neuron_clib_";
    filename += std::to_string(nn_id);
    for (size_t i = 0; i < 64; ++i) {
        if (Env::Default()->CreateUniqueFileName(&filename, "")) {
            return filename;
        }
        Env::Default()->SleepForMicroseconds(1);
    }
    return "";
}

Status SharedMemoryManager::initialize(const std::string &nrtd_address,
                                       const uint32_t nn_id,
                                       const std::vector<size_t> &input_tensor_sizes,
                                       const std::vector<size_t> &output_tensor_sizes) {
    TF_RETURN_IF_ERROR(init_stub(&stub_, nrtd_address));
    TF_RETURN_IF_ERROR(init_vectors(&input_names_, &input_ptrs_, &input_sizes_,
                                    &input_grpc_names_, input_tensor_sizes, nn_id));
    TF_RETURN_IF_ERROR(init_vectors(&output_names_, &output_ptrs_, &output_sizes_,
                                    &output_grpc_names_, output_tensor_sizes, nn_id));
    for (size_t idx = 0; idx < input_names_.size(); ++idx) {
        VLOG(1) << "input shared memory " << input_names_[idx]
                << " ready at address " << input_ptrs_[idx];
    }
    for (size_t idx = 0; idx < output_names_.size(); ++idx) {
        VLOG(1) << "output shared memory " << output_names_[idx]
                << " ready at address " << output_ptrs_[idx];
    }
    enabled_ = true;
    return Status::OK();
}

Status SharedMemoryManager::init_vectors(std::vector<std::string> *names,
                                         std::vector<void*> *ptrs,
                                         std::vector<size_t> *sizes,
                                         std::vector<std::string> *grpc_names,
                                         const std::vector<size_t> &tensor_sizes,
                                         const uint32_t nn_id) {
    for (size_t size : tensor_sizes) {
        std::string name = gen_shm_name(nn_id);
        if (name.empty()) {
            return errors::Internal("cannot generate unique file name for shared memory");
        }
        int shm_fd = ::shm_open(name.c_str(), O_CREAT | O_RDWR, S_IRWXU | S_IRWXG | S_IRWXO);
        SYS_FAIL_RETURN(shm_fd < 0, "shm_open");
        names->push_back(name);
        SYS_FAIL_RETURN(::ftruncate(shm_fd, size) < 0, "ftruncate");
        void *ptr = ::mmap(0, size, PROT_WRITE, MAP_SHARED, shm_fd, 0);
        SYS_FAIL_RETURN(nullptr == ptr, "mmap");
        ptrs->push_back(ptr);
        sizes->push_back(size);
        nrt::shm_map_request request;
        request.set_path(name);
        request.set_mmap_prot(PROT_READ | PROT_WRITE);
        nrt::shm_map_response response;
        grpc::Status status = NRT_GRPC(stub_->shm_map, request, &response);
        NRT_CHECK_RETURN("shm_map", status, response);
        grpc_names->push_back(name);
    }
    return Status::OK();
}

SharedMemoryManager::~SharedMemoryManager() {
    for (const auto &name : input_grpc_names_) {
        nrt_shm_unmap(name);
    }
    input_grpc_names_.clear();
    for (size_t idx = 0; idx < input_ptrs_.size(); ++idx) {
        SYS_FAIL_LOG(munmap(input_ptrs_[idx], input_sizes_[idx]) < 0, "munmap");
    }
    input_ptrs_.clear();
    for (const auto &name : input_names_) {
        SYS_FAIL_LOG(shm_unlink(name.c_str()) < 0, "shm_unlink");
    }
    input_names_.clear();
    for (const auto &name : output_grpc_names_) {
        nrt_shm_unmap(name);
    }
    output_grpc_names_.clear();
    for (size_t idx = 0; idx < output_ptrs_.size(); ++idx) {
        SYS_FAIL_LOG(munmap(output_ptrs_[idx], output_sizes_[idx]) < 0, "munmap");
    }
    output_ptrs_.clear();
    for (const auto &name : output_names_) {
        SYS_FAIL_LOG(shm_unlink(name.c_str()) < 0, "shm_unlink");
    }
    output_names_.clear();
}

void SharedMemoryManager::nrt_shm_unmap(const std::string &name) {
    nrt::shm_unmap_request request;
    request.set_path(name);
    request.set_mmap_prot(PROT_READ | PROT_WRITE);
    nrt::shm_unmap_response response;
    grpc::Status status = NRT_GRPC(stub_->shm_unmap, request, &response);
    NRT_CHECK_LOG("shm_unmap", status, response);
}


static std::string remove_pattern(std::string data, const std::string &pattern) {
    size_t string_length = data.size();
    for (size_t idx = 0; idx < string_length; ++idx) {
        size_t pos = data.find(pattern, pos);
        if (std::string::npos == pos) {
            break;
        }
        data.replace(pos, pattern.size(), "");
    }
    return data;
}

NeuronDeviceManager::~NeuronDeviceManager() {
    tensorflow::mutex_lock lock(global_mutex_);
    clear();
}

Status NeuronDeviceManager::initialize(int64_t opt_device_size) {
    if (!path_set_) {
        // append /opt/aws/neuron/bin to PATH
        std::string env_path = env_get("PATH", "");
        setenv("PATH", (env_path + ":/opt/aws/neuron/bin").c_str(), 1);
        path_set_ = true;
    }

    // stub
    nrtd_address_ = env_get("NEURON_RTD_ADDRESS", "unix:/run/neuron.sock");
    TF_RETURN_IF_ERROR(init_stub(&stub_, nrtd_address_));

    // get number of neuron cores from comma-separated list of integers
    std::string neuron_device_sizes_raw = env_get("NEURONCORE_GROUP_SIZES", "");
    if (neuron_device_sizes_raw.empty()) {
        TF_RETURN_IF_ERROR(init_default_device(opt_device_size));
    } else {
        // remove [ and ]
        std::string neuron_device_sizes = remove_pattern(neuron_device_sizes_raw, "[");
        neuron_device_sizes = remove_pattern(neuron_device_sizes, "]");

        std::vector<int> num_cores_req_vector;
        std::stringstream neuron_device_sizes_stream(neuron_device_sizes);
        for (size_t idx = 0; idx < MAX_NUM_CORES; ++idx) {
            if (!neuron_device_sizes_stream.good()) {
                break;
            }
            std::string substr;
            std::getline(neuron_device_sizes_stream, substr, ',');
            if (substr.empty()) {
                continue;
            }
            int num_cores_req = stoi_no_throw(substr);
            if (num_cores_req < 0 || num_cores_req > 64) {
                LOG(WARNING) << "NEURONCORE_GROUP_SIZES=" << neuron_device_sizes_raw
                             << " looks ill-formatted. Falling back to initializing"
                             << " a default NeuronCore Group.";
                num_cores_req_vector.clear();
                break;
            }
            num_cores_req_vector.push_back(num_cores_req);
        }
        if (num_cores_req_vector.empty()) {
            TF_RETURN_IF_ERROR(init_default_device(opt_device_size));
        } else {
            TF_RETURN_IF_ERROR(init_devices(num_cores_req_vector));
        }
    }
    ready_ = true;
    return Status::OK();
}

Status NeuronDeviceManager::init_devices(const std::vector<int> &num_cores_req_vector) {
    Status status = errors::Internal("No NeuronCore Group can be initialized.");
    for (size_t idx = 0; idx < num_cores_req_vector.size(); ++idx) {
        int num_cores_req = num_cores_req_vector[idx];
        status = device_array_[idx].initialize(nrtd_address_, num_cores_req);
        if (!status.ok()) {
            LOG(WARNING) << "Cannot initialize NeuronCore Group with " << num_cores_req
                         << " cores; stopping initialization.";
            break;
        }
        ++num_devices_;
        VLOG(1) << "successfully initialized NeuronCore Group of size " << num_cores_req;
    }
    if (0 == num_devices_) {
        return status;
    }
    return Status::OK();
}

Status NeuronDeviceManager::init_default_device(int64_t opt_device_size) {
    if (opt_device_size < 0 || opt_device_size > 64) {
        // device size looks wrong -- just get the largest ncg possible
        Status status = device_array_[0].initialize(nrtd_address_, DEFAULT_NUM_CORES);
        num_devices_ = status.ok() ? 1 : 0;
        return status;
    } else {
        // get one full Inferentia by default
        if (opt_device_size == 1) {
            std::vector<int> num_cores_req_vector({1, 1, 1, 1});
            TF_RETURN_IF_ERROR(init_devices(num_cores_req_vector));
        } else if (opt_device_size == 2) {
            std::vector<int> num_cores_req_vector({2, 2});
            TF_RETURN_IF_ERROR(init_devices(num_cores_req_vector));
        } else {
            // search for the largest possible ncg ... sorry
            Status status = errors::Internal("No NeuronCore Group can be initialized.");
            for (int num_cores = opt_device_size; num_cores >= MIN_NUM_CORES; --num_cores) {
                status = device_array_[0].initialize(nrtd_address_, num_cores);
                if (status.ok()) {
                    num_devices_ = 1;
                    return status;
                }
            }
            num_devices_ = 0;
            return status;
        }
    }
    return Status::OK();
}

Status NeuronDeviceManager::clear_if_empty() {
    tensorflow::mutex_lock lock(global_mutex_);
    bool empty = true;
    for (size_t idx = 0; idx < num_devices_; ++idx) {
        if (0 != device_array_[idx].num_executable()) {
            empty = false;
        }
    }
    if (empty) {
        clear();
    }
    return Status::OK();
}

void NeuronDeviceManager::clear() {
    for (size_t idx = 0; idx < num_devices_; ++idx) {
        device_array_[idx].clear();
    }
    num_devices_ = 0;
    device_index_ = 0;
    ready_ = false;
    VLOG(1) << "NeuronDeviceManager is cleared";
}

Status NeuronDeviceManager::apply_for_device(NeuronDevice **device,
                                             int64_t opt_device_size) {
    tensorflow::mutex_lock lock(global_mutex_);
    if (!ready_) {
        TF_RETURN_IF_ERROR(initialize(opt_device_size));
#ifdef NEURONTFSERV
        std::signal(SIGINT, sigint_handler);
        std::signal(SIGTERM, sigint_handler);
#endif // NEURONTFSERV
    }

    *device = &device_array_[device_index_];
    ++device_index_;
    if (device_index_ >= num_devices_) {
        device_index_ = 0;
    }
    return Status::OK();
}

Status NeuronDevice::initialize(const std::string &nrtd_address, int num_cores_req) {
    nrtd_address_ = nrtd_address;
    TF_RETURN_IF_ERROR(init_stub(&stub_, nrtd_address_));
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
    num_cores_ = response.nc_count();
    eg_id_ = response.h_eg().id();
    create_eg_done_ = true;
    running_nn_id_ = NRT_INVALID_NN_ID;
    return Status::OK();
}

Status NeuronDevice::load(uint32_t *nn_id, const StringPiece &executable,
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
    request.mutable_h_eg()->set_id(eg_id_);
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
    tensorflow::mutex_lock lock(mutex_eg_);
    nn_id_set_.insert(response.h_nn().id());
    *nn_id = response.h_nn().id();
    return Status::OK();
}

void NeuronDevice::unload(const uint32_t nn_id) {
    {
        tensorflow::mutex_lock lock(mutex_eg_);
        nn_id_set_.erase(nn_id);
        // stop
        if (running(nn_id)) {
            nrt::stop_request request;
            request.mutable_h_nn()->set_id(nn_id);
            nrt::stop_response response;
            grpc::Status status = NRT_GRPC(stub_->stop, request, &response);
            NRT_CHECK_LOG("stop", status, response);
            set_running(NRT_INVALID_NN_ID);
        }
    }

    // unload
    if (NRT_INVALID_NN_ID != nn_id) {
        nrt::unload_request request;
        request.mutable_h_nn()->set_id(nn_id);
        nrt::unload_response response;
        grpc::Status status = NRT_GRPC(stub_->unload, request, &response);
        NRT_CHECK_LOG("unload", status, response);
    }
    VLOG(1) << "unload: number of NEFFs: " << num_executable();
}

Status NeuronDevice::infer(std::vector<Tensor*> *output_tensors, Timestamps *timestamps,
                           ProfilerInterface *profile, const uint32_t nn_id,
                           AttrList &input_names, AttrList &output_names,
                           const std::vector<const Tensor*> &input_tensors,
                           const SharedMemoryManager &shm) {
    tensorflow::mutex_lock lock(mutex_eg_);
    TF_RETURN_IF_ERROR(start_model(nn_id));
    if (profile->enabled_) profile->start_session(nrtd_address_, nn_id);
    nrt::infer_request request;
    for (int idx = 0; idx < input_names.s_size(); ++idx) {
        nrt::infer_io *infer_io = request.add_ifmap();
        infer_io->set_name(input_names.s(idx));
        StringPiece tensor_data(input_tensors[idx]->tensor_data());
        if (shm.enabled_) {
            infer_io->mutable_buf_shm()->set_path(shm.input_names_[idx]);
            std::memcpy(shm.input_ptrs_[idx], tensor_data.data(), tensor_data.size());
        } else {
            infer_io->set_buf(tensor_data.data(), tensor_data.size());
        }
    }
    if (shm.enabled_) {
        for (int idx = 0; idx < output_names.s_size(); ++idx) {
            nrt::infer_io *infer_io = request.add_shm_ofmap();
            infer_io->set_name(output_names.s(idx));
            infer_io->mutable_buf_shm()->set_path(shm.output_names_[idx]);
        }
    }
    request.mutable_h_nn()->set_id(nn_id);
    nrt::infer_response response;

    // infer
    timestamps->mark_above_nrtd_infer();
    grpc::Status status = NRT_GRPC(stub_->infer, request, &response);
    timestamps->mark_below_nrtd_infer();
    if (status.ok()) {
        // ignore inf/nan errors
        if (nrt::nerr::NERR_INFER_COMPLETED_WITH_NUM_ERR == response.status().code()) {
            response.mutable_status()->set_code(nrt::nerr::NERR_OK);
        }
    }
    if (profile->enabled_) profile->stop_session();
    NRT_CHECK_RETURN("infer", status, response);
    if (shm.enabled_) {
        for (int idx = 0; idx < output_names.s_size(); ++idx) {
            nrt::infer_io *infer_io = response.add_ofmap();
            infer_io->set_name(output_names.s(idx));
            infer_io->set_buf(shm.output_ptrs_[idx], shm.output_sizes_[idx]);
        }
    }
    TF_RETURN_IF_ERROR(copy_output_tensors(output_tensors, response, output_names));
    return Status::OK();
}

Status NeuronDevice::infer_post(uint64_t *cookie, SemResQueue *sem_res_queue,
                                xla::Semaphore *infer_sem, Timestamps *timestamps,
                                const uint32_t nn_id, AttrList &input_names,
                                const std::vector<const Tensor*> &input_tensors) {
    tensorflow::mutex_lock lock(mutex_eg_);
    sem_res_queue->push(infer_sem->ScopedAcquire(1));
    return infer_post_unsafe(cookie, timestamps, nn_id, input_names, input_tensors);
}

void NeuronDevice::acquire_mutex(std::queue<tensorflow::mutex_lock> *mutex_lock_queue) {
    mutex_lock_queue->emplace(mutex_eg_);
}

Status NeuronDevice::infer_post_unsafe(uint64_t *cookie, Timestamps *timestamps,
                                       const uint32_t nn_id, AttrList &input_names,
                                       const std::vector<const Tensor*> &input_tensors) {
    TF_RETURN_IF_ERROR(start_model(nn_id));
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
    *cookie = response.cookie();
    return Status::OK();
}

Status NeuronDevice::infer_wait(std::vector<Tensor*> *output_tensors,
                                Timestamps *timestamps,
                                const uint64_t cookie, AttrList &output_names) {
    nrt::infer_wait_request request;
    nrt::infer_response response;
    request.set_cookie(cookie);

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

void NeuronDevice::clear() {
    tensorflow::mutex_lock lock(mutex_eg_);
    for (uint32_t nn_id : nn_id_set_) {
        // stop
        if (running(nn_id)) {
            nrt::stop_request request;
            request.mutable_h_nn()->set_id(nn_id);
            nrt::stop_response response;
            grpc::Status status = NRT_GRPC(stub_->stop, request, &response);
            NRT_CHECK_LOG("stop", status, response);
            set_running(NRT_INVALID_NN_ID);
        }

        // unload
        nrt::unload_request request;
        request.mutable_h_nn()->set_id(nn_id);
        nrt::unload_response response;
        grpc::Status status = NRT_GRPC(stub_->unload, request, &response);
        NRT_CHECK_LOG("unload", status, response);
        VLOG(1) << "unload from NeuronDevice::clear";
    }
    nn_id_set_.clear();
    if (create_eg_done_) {
        // destroy_eg
        nrt::destroy_eg_request request;
        request.mutable_h_eg()->set_id(eg_id_);
        nrt::destroy_eg_response response;
        grpc::Status status = NRT_GRPC(stub_->destroy_eg, request, &response);
        NRT_CHECK_LOG("destroy_eg", status, response);
        create_eg_done_ = false;
        VLOG(1) << "destroy_eg from NeuronDevice::clear";
    }
}

Status NeuronDevice::start_model(const uint32_t nn_id) {
    if (!create_eg_done_) {
        return errors::Internal("neuron_device is not initialized");
    }
    if (!running(nn_id) && is_busy()) {
        // if nn_id is not running, stop the current running model
        nrt::stop_request request;
        request.mutable_h_nn()->set_id(nn_get_current_running());
        nrt::stop_response response;
        grpc::Status status = NRT_GRPC(stub_->stop, request, &response);
        NRT_CHECK_RETURN("stop", status, response);
        set_running(NRT_INVALID_NN_ID);
    }
    if (!is_busy()) {
        // if no model is running, start nn_id
        nrt::start_request request;
        request.mutable_h_nn()->set_id(nn_id);
        nrt::start_response response;
        grpc::Status status = NRT_GRPC(stub_->start, request, &response);
        NRT_CHECK_RETURN("start", status, response);
        set_running(nn_id);
    }
    return Status::OK();
}

bool NeuronDevice::is_busy() {
    return running_nn_id_ != NRT_INVALID_NN_ID;
}

bool NeuronDevice::running(uint32_t nn_id) {
    return running_nn_id_ == nn_id && NRT_INVALID_NN_ID != running_nn_id_;
}

uint32_t NeuronDevice::nn_get_current_running() {
    return running_nn_id_;
}

void NeuronDevice::set_running(uint32_t nn_id) {
    running_nn_id_ = nn_id;
}

Status NeuronDevice::copy_output_tensors(std::vector<Tensor*> *output_tensors,
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


std::string env_get(const char *env_var, const char *default_env_var) {
    char *str = std::getenv(env_var);
    return str ? str : default_env_var;
}

int stoi_no_throw(const std::string &str) {
    try {
        return std::stoi(str);
    } catch (std::invalid_argument e) {
        return -1;
    } catch (std::out_of_range e) {
        return -1;
    }
}

Status init_stub(std::unique_ptr<nrt::nmgr_v1::Stub> *stub,
                 const std::string &nrtd_address) {
    grpc::ChannelArguments ch_args;
    ch_args.SetMaxReceiveMessageSize(-1);
    ch_args.SetMaxSendMessageSize(-1);
    std::shared_ptr<grpc::Channel> channel = grpc::CreateCustomChannel(
        nrtd_address, grpc::InsecureChannelCredentials(), ch_args);
    if (!channel) {
        return errors::Unavailable(
            "cannot establish grpc channel to neuron-rtd server");
    }
    *stub = nrt::nmgr_v1::NewStub(channel);
    if (!(*stub)) {
        return errors::Unavailable("cannot create stub");
    }
    return Status::OK();
}


}  // namespace neuron
}  // namespace tensorflow
