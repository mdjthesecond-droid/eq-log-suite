// Minimal Vulkan implicit layer for eq-log-suite's standalone
// (non-MangoHud-derived) alert overlay. Renders one draggable-by-companion-
// app box per active alert key, consuming tailer.py's real alert-broadcast
// socket (see overlay_state.h's shape-dispatch comment for the four alert
// shapes this covers) on top of whatever Vulkan app loads this layer.
// Written against public Vulkan layer conventions
// (docs.vulkan.org / LunarG's Vulkan-Tools sample layers), not copied from
// any existing overlay project.
//
// Simplification used throughout: this layer links directly against
// libvulkan.so (see meson.build) and calls the plain vkFoo() entry points
// for anything it has NOT intercepted - those route correctly through the
// loader's own per-object dispatch table. Only for the handful of
// functions this layer DOES intercept (listed in kLayerName's exported
// GetInstanceProcAddr/GetDeviceProcAddr below) does it call through the
// "next" function pointer captured from the loader's layer-chain info,
// to avoid recursing into itself.

#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>

#include "imgui.h"
#include "imgui_impl_vulkan.h"

#include "overlay_state.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

constexpr const char* kLayerName = "VK_LAYER_EQLOG_overlay";

// ---- Per-device state -------------------------------------------------

struct DeviceData {
    VkDevice device = VK_NULL_HANDLE;
    VkPhysicalDevice physicalDevice = VK_NULL_HANDLE;
    VkInstance instance = VK_NULL_HANDLE;
    VkQueue graphicsQueue = VK_NULL_HANDLE;
    uint32_t graphicsQueueFamily = 0;

    PFN_vkGetDeviceProcAddr nextGetDeviceProcAddr = nullptr;
    PFN_vkGetInstanceProcAddr nextGetInstanceProcAddr = nullptr; // this instance's chain, cached to avoid
                                                                  // re-locking g_mapMutex from contexts that
                                                                  // already hold it (e.g. the ImGui loader
                                                                  // callback, called from within
                                                                  // Overlay_QueuePresentKHR's own lock_guard).
    PFN_vkCreateSwapchainKHR nextCreateSwapchainKHR = nullptr;
    PFN_vkDestroySwapchainKHR nextDestroySwapchainKHR = nullptr;
    PFN_vkQueuePresentKHR nextQueuePresentKHR = nullptr;
};

struct SwapchainData {
    VkDevice device = VK_NULL_HANDLE;
    VkExtent2D extent{};
    VkFormat format = VK_FORMAT_UNDEFINED;

    std::vector<VkImage> images;
    std::vector<VkImageView> imageViews;
    std::vector<VkFramebuffer> framebuffers;
    std::vector<VkCommandBuffer> commandBuffers;
    std::vector<VkFence> fences;

    VkRenderPass renderPass = VK_NULL_HANDLE;
    VkCommandPool commandPool = VK_NULL_HANDLE;
    VkDescriptorPool imguiDescriptorPool = VK_NULL_HANDLE;
    ImGuiContext* imguiContext = nullptr;
    bool imguiBackendInitialized = false;
};

std::mutex g_mapMutex;
std::unordered_map<VkDevice, DeviceData> g_deviceMap;
std::unordered_map<VkSwapchainKHR, SwapchainData> g_swapchainMap;
std::unordered_map<VkPhysicalDevice, VkInstance> g_physDeviceToInstance;
// Per-instance "next" GetInstanceProcAddr - see the dispatch-safety note
// above InitSwapchainResources. Used by Overlay_GetInstanceProcAddr's
// fallback so it never has to call the globally-linked vkGetInstanceProcAddr.
std::unordered_map<VkInstance, PFN_vkGetInstanceProcAddr> g_instanceNextGipa;

// ---- Helpers ------------------------------------------------------------

// NOTE ON DISPATCH SAFETY: every call in this section takes an explicit
// "next"-chain function pointer (captured from the loader during
// CreateInstance/CreateDevice) rather than calling the globally-linked
// vkFoo() symbol. This matters specifically here, not elsewhere in the
// file: the loader holds an internal, non-reentrant lock for the full
// duration of the *outer* vkCreateInstance()/vkCreateDevice() trampoline
// call an application makes - and our hook functions run nested inside
// that same call, before it returns to the app. Issuing any additional
// Vulkan call through the globally-linked symbol from in here re-enters
// the loader's dispatch machinery while that lock is still held and
// deadlocks (confirmed live: hung in a futex wait inside
// vkEnumeratePhysicalDevices when this used the global symbol). Calls
// made later, e.g. from the vkQueuePresentKHR hook, are NOT nested inside
// an in-progress instance/device-creation call, so no such lock is held
// there - global linkage is fine in InitSwapchainResources/DrawOverlayFrame.
uint32_t FindGraphicsQueueFamily(PFN_vkGetPhysicalDeviceQueueFamilyProperties nextGetQFP, VkPhysicalDevice pd) {
    uint32_t count = 0;
    nextGetQFP(pd, &count, nullptr);
    std::vector<VkQueueFamilyProperties> props(count);
    nextGetQFP(pd, &count, props.data());
    for (uint32_t i = 0; i < count; i++) {
        if (props[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) return i;
    }
    return 0;
}

// Custom loader for Dear ImGui's Vulkan backend (imgui_impl_vulkan.cpp is
// compiled with IMGUI_IMPL_VULKAN_NO_PROTOTYPES - see meson.build - so it
// calls nothing via static linkage). Resolves every function ImGui asks
// for via this layer's own captured "next"-chain pointers instead of the
// globally-linked vk* symbols, for the same dispatch-safety reason
// documented above: the global symbols route through the loader's
// top-level trampoline, which rejects this layer's physical device handle
// for calls like vkGetPhysicalDeviceProperties (confirmed live: the
// identical handle works via the chained pointer, aborts via global
// linkage) even with zero other layers in the chain.
PFN_vkVoidFunction ImGuiVulkanLoaderFunc(const char* name, void* userData) {
    auto* dd = static_cast<DeviceData*>(userData);
    if (auto fn = dd->nextGetDeviceProcAddr(dd->device, name)) return fn;
    // g_mapMutex is intentionally NOT touched here - this callback runs
    // nested inside Overlay_QueuePresentKHR's own lock_guard on that same
    // mutex (via InitSwapchainResources), so re-locking it would deadlock
    // (confirmed live: hung with no crash/output, classic non-recursive
    // self-deadlock). dd->nextGetInstanceProcAddr is cached on DeviceData
    // at CreateDevice time specifically so this path never needs the lock.
    return dd->nextGetInstanceProcAddr(dd->instance, name);
}

void DestroySwapchainResources(SwapchainData& sc) {
    if (sc.device == VK_NULL_HANDLE) return;
    if (sc.imguiBackendInitialized) {
        ImGui::SetCurrentContext(sc.imguiContext);
        ImGui_ImplVulkan_Shutdown();
    }
    if (sc.imguiContext) ImGui::DestroyContext(sc.imguiContext);

    for (auto fb : sc.framebuffers) vkDestroyFramebuffer(sc.device, fb, nullptr);
    for (auto iv : sc.imageViews) vkDestroyImageView(sc.device, iv, nullptr);
    if (sc.renderPass) vkDestroyRenderPass(sc.device, sc.renderPass, nullptr);
    if (sc.imguiDescriptorPool) vkDestroyDescriptorPool(sc.device, sc.imguiDescriptorPool, nullptr);
    for (auto f : sc.fences) vkDestroyFence(sc.device, f, nullptr);
    if (sc.commandPool) vkDestroyCommandPool(sc.device, sc.commandPool, nullptr);

    sc = SwapchainData{};
}

// Builds render targets + an ImGui Vulkan backend bound to this swapchain's
// images. Called lazily on first present of a given swapchain.
void InitSwapchainResources(SwapchainData& sc, DeviceData& dd, VkSwapchainKHR swapchain) {
    VkDevice device = dd.device;

    uint32_t imageCount = 0;
    vkGetSwapchainImagesKHR(device, swapchain, &imageCount, nullptr);
    sc.images.resize(imageCount);
    vkGetSwapchainImagesKHR(device, swapchain, &imageCount, sc.images.data());

    VkAttachmentDescription attachment{};
    attachment.format = sc.format;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    VkAttachmentReference colorRef{0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorRef;

    VkSubpassDependency dep{};
    dep.srcSubpass = VK_SUBPASS_EXTERNAL;
    dep.dstSubpass = 0;
    dep.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dep.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dep.srcAccessMask = 0;
    dep.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;

    VkRenderPassCreateInfo rpInfo{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
    rpInfo.attachmentCount = 1;
    rpInfo.pAttachments = &attachment;
    rpInfo.subpassCount = 1;
    rpInfo.pSubpasses = &subpass;
    rpInfo.dependencyCount = 1;
    rpInfo.pDependencies = &dep;
    vkCreateRenderPass(device, &rpInfo, nullptr, &sc.renderPass);

    sc.imageViews.resize(imageCount);
    sc.framebuffers.resize(imageCount);
    for (uint32_t i = 0; i < imageCount; i++) {
        VkImageViewCreateInfo ivInfo{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
        ivInfo.image = sc.images[i];
        ivInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
        ivInfo.format = sc.format;
        ivInfo.components = {VK_COMPONENT_SWIZZLE_IDENTITY, VK_COMPONENT_SWIZZLE_IDENTITY,
                              VK_COMPONENT_SWIZZLE_IDENTITY, VK_COMPONENT_SWIZZLE_IDENTITY};
        ivInfo.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        vkCreateImageView(device, &ivInfo, nullptr, &sc.imageViews[i]);

        VkFramebufferCreateInfo fbInfo{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
        fbInfo.renderPass = sc.renderPass;
        fbInfo.attachmentCount = 1;
        fbInfo.pAttachments = &sc.imageViews[i];
        fbInfo.width = sc.extent.width;
        fbInfo.height = sc.extent.height;
        fbInfo.layers = 1;
        vkCreateFramebuffer(device, &fbInfo, nullptr, &sc.framebuffers[i]);
    }

    VkCommandPoolCreateInfo poolInfo{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    poolInfo.queueFamilyIndex = dd.graphicsQueueFamily;
    vkCreateCommandPool(device, &poolInfo, nullptr, &sc.commandPool);

    sc.commandBuffers.resize(imageCount);
    VkCommandBufferAllocateInfo cbInfo{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbInfo.commandPool = sc.commandPool;
    cbInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbInfo.commandBufferCount = imageCount;
    vkAllocateCommandBuffers(device, &cbInfo, sc.commandBuffers.data());

    sc.fences.resize(imageCount);
    VkFenceCreateInfo fenceInfo{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    for (uint32_t i = 0; i < imageCount; i++) vkCreateFence(device, &fenceInfo, nullptr, &sc.fences[i]);

    VkDescriptorPoolSize poolSize{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 4};
    VkDescriptorPoolCreateInfo dpInfo{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dpInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    dpInfo.maxSets = 4;
    dpInfo.poolSizeCount = 1;
    dpInfo.pPoolSizes = &poolSize;
    vkCreateDescriptorPool(device, &dpInfo, nullptr, &sc.imguiDescriptorPool);

    sc.imguiContext = ImGui::CreateContext();
    ImGui::SetCurrentContext(sc.imguiContext);
    // Disable ImGui's own window-position ini persistence: left at its
    // default, it silently writes "imgui.ini" into whatever the hooked
    // game process's cwd happens to be (confirmed live: landed in the
    // repo root during this phase's vkcube testing) - a second,
    // uncoordinated persistence mechanism competing with our own
    // deliberate one (overlay_ipc.cpp's kPositionFileRelPath). Same
    // reasoning MangoHud itself uses for disabling this.
    ImGui::GetIO().IniFilename = nullptr;
    ImGui::GetIO().DisplaySize = ImVec2((float)sc.extent.width, (float)sc.extent.height);
    // No windowing backend (GLFW/SDL) is attached - this layer draws inside
    // the game's own swapchain, so ImGui never owns an OS window. Boxes are
    // NoMove by design (see DrawOverlayFrame below): the companion app
    // controls position over IPC instead, so this layer never needs real
    // host input passthrough at all.

    ImGui_ImplVulkan_InitInfo initInfo{};
    initInfo.ApiVersion = VK_API_VERSION_1_3;
    initInfo.Instance = dd.instance;
    initInfo.PhysicalDevice = dd.physicalDevice;
    initInfo.Device = dd.device;
    initInfo.QueueFamily = dd.graphicsQueueFamily;
    initInfo.Queue = dd.graphicsQueue;
    initInfo.DescriptorPool = sc.imguiDescriptorPool;
    initInfo.MinImageCount = imageCount;
    initInfo.ImageCount = imageCount;
    initInfo.PipelineInfoMain.RenderPass = sc.renderPass;
    initInfo.PipelineInfoMain.MSAASamples = VK_SAMPLE_COUNT_1_BIT;

    ImGui_ImplVulkan_LoadFunctions(VK_API_VERSION_1_3, ImGuiVulkanLoaderFunc, &dd);
    ImGui_ImplVulkan_Init(&initInfo);
    // Font atlas upload is automatic in this ImGui version - handled via
    // ImDrawData::Textures during the first ImGui_ImplVulkan_RenderDrawData
    // call, no explicit CreateFontsTexture() call needed.

    sc.imguiBackendInitialized = true;
    fprintf(stderr, "[eq_overlay] ImGui Vulkan backend initialized for swapchain (%ux%u, %u images)\n",
            sc.extent.width, sc.extent.height, imageCount);
}

// Directional cues (shape 2) render as a large glyph+word rather than a
// literal arrow polygon - keeps this phase to ImGui text APIs only, no
// custom draw-list geometry. "left"/"right"/"front"/"behind" match the
// vocabulary raid-mechanic guides actually use (see [[project_overlay_v2_
// alert_boxes]]'s Rikkukin example); anything else falls back to its
// own uppercased text so a not-yet-anticipated direction string still
// shows something instead of silently rendering blank.
std::string DirectionGlyph(const std::string& direction) {
    if (direction == "left") return "<<< LEFT";
    if (direction == "right") return "RIGHT >>>";
    if (direction == "front") return "^^^ FRONT";
    if (direction == "behind") return "BEHIND vvv";
    std::string upper = direction;
    for (auto& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return upper;
}

void DrawOverlayFrame(SwapchainData& sc, DeviceData& dd, uint32_t imageIndex) {
    ImGui::SetCurrentContext(sc.imguiContext);
    ImGui_ImplVulkan_NewFrame();
    ImGui::NewFrame();

    // Prune expired boxes and snapshot the rest under the lock, then draw
    // from the snapshot with the lock released - same rationale as
    // Overlay_QueuePresentKHR's lock-then-release-then-render pattern
    // above (rendering can re-enter this layer's own GetDeviceProcAddr via
    // the loader's extension-resolution path; see that function's comment
    // for the confirmed-live deadlock this avoids).
    std::vector<AlertBox> snapshot;
    {
        OverlayState& state = GetOverlayState();
        std::lock_guard<std::mutex> lock(state.mutex);
        auto now = std::chrono::steady_clock::now();
        for (auto it = state.boxes.begin(); it != state.boxes.end();) {
            const AlertBox& box = it->second;
            if (box.hasDuration) {
                double elapsed = std::chrono::duration<double>(now - box.receivedAt).count();
                if (elapsed > box.durationSeconds) {
                    it = state.boxes.erase(it);
                    continue;
                }
            }
            snapshot.push_back(box);
            ++it;
        }
    }

    auto now = std::chrono::steady_clock::now();
    for (const AlertBox& box : snapshot) {
        ImGui::SetNextWindowPos(ImVec2(box.x, box.y), ImGuiCond_Always);
        ImGui::SetNextWindowBgAlpha(0.85f);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(box.color[0], box.color[1], box.color[2], 1.0f));
        std::string windowId = "##eq_overlay_box_" + box.key;
        ImGui::Begin(windowId.c_str(), nullptr,
                      ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                          ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoMove);

        if (box.hasDirection) {
            // Shape 2: instant directional cue.
            ImGui::SetWindowFontScale(1.8f);
            ImGui::TextUnformatted(DirectionGlyph(box.direction).c_str());
            ImGui::SetWindowFontScale(1.0f);
            if (!box.text.empty()) ImGui::TextUnformatted(box.text.c_str());
        } else {
            ImGui::TextUnformatted(box.text.empty() ? box.key.c_str() : box.text.c_str());
        }

        if (box.countdown && box.hasDuration) {
            // Shape 1: countdown-with-progress-bar, draining full -> empty.
            double elapsed = std::chrono::duration<double>(now - box.receivedAt).count();
            float progress = box.durationSeconds > 0.0f
                                  ? std::clamp(1.0f - static_cast<float>(elapsed) / box.durationSeconds, 0.0f, 1.0f)
                                  : 0.0f;
            ImGui::ProgressBar(progress, ImVec2(200, 0));
        }
        // Shape 3 (sticky, !hasDuration) and shape 4 (plain timed alert)
        // need no extra widget beyond the text above - they differ only in
        // how/when DrawOverlayFrame's pruning loop above removes them
        // (explicit "clear" vs. natural duration expiry).

        ImGui::End();
        ImGui::PopStyleColor();
    }

    ImGui::Render();
    ImDrawData* drawData = ImGui::GetDrawData();

    VkFence fence = sc.fences[imageIndex];
    vkWaitForFences(dd.device, 1, &fence, VK_TRUE, UINT64_MAX);
    vkResetFences(dd.device, 1, &fence);

    VkCommandBuffer cmd = sc.commandBuffers[imageIndex];
    vkResetCommandBuffer(cmd, 0);

    VkCommandBufferBeginInfo beginInfo{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    vkBeginCommandBuffer(cmd, &beginInfo);

    VkClearValue clearValue{}; // unused (LOAD_OP_LOAD): keeps whatever the game already rendered
    VkRenderPassBeginInfo rpBegin{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
    rpBegin.renderPass = sc.renderPass;
    rpBegin.framebuffer = sc.framebuffers[imageIndex];
    rpBegin.renderArea.extent = sc.extent;
    rpBegin.clearValueCount = 1;
    rpBegin.pClearValues = &clearValue;
    vkCmdBeginRenderPass(cmd, &rpBegin, VK_SUBPASS_CONTENTS_INLINE);

    ImGui_ImplVulkan_RenderDrawData(drawData, cmd);

    vkCmdEndRenderPass(cmd);
    vkEndCommandBuffer(cmd);

    VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &cmd;
    vkQueueSubmit(dd.graphicsQueue, 1, &submit, fence);
    // Simplicity over performance for the spike: block until our overlay
    // draw is done before handing back to the real present call, instead
    // of threading a semaphore into pPresentInfo's wait list.
    vkWaitForFences(dd.device, 1, &fence, VK_TRUE, UINT64_MAX);
}

// ---- Intercepted entry points ------------------------------------------

VKAPI_ATTR VkResult VKAPI_CALL Overlay_CreateInstance(const VkInstanceCreateInfo* pCreateInfo,
                                                       const VkAllocationCallbacks* pAllocator,
                                                       VkInstance* pInstance) {
    auto* chainInfo = (VkLayerInstanceCreateInfo*)pCreateInfo->pNext;
    while (chainInfo && !(chainInfo->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO &&
                           chainInfo->function == VK_LAYER_LINK_INFO)) {
        chainInfo = (VkLayerInstanceCreateInfo*)chainInfo->pNext;
    }
    if (!chainInfo) return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr gpa = chainInfo->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    chainInfo->u.pLayerInfo = chainInfo->u.pLayerInfo->pNext;

    auto createFunc = (PFN_vkCreateInstance)gpa(nullptr, "vkCreateInstance");
    VkResult result = createFunc(pCreateInfo, pAllocator, pInstance);
    if (result != VK_SUCCESS) return result;

    // Resolved via gpa (the "next" chain pointer), not the global linked
    // symbol - see the dispatch-safety note above InitSwapchainResources.
    auto nextEnumeratePhysicalDevices =
        (PFN_vkEnumeratePhysicalDevices)gpa(*pInstance, "vkEnumeratePhysicalDevices");

    uint32_t count = 0;
    nextEnumeratePhysicalDevices(*pInstance, &count, nullptr);
    std::vector<VkPhysicalDevice> devices(count);
    nextEnumeratePhysicalDevices(*pInstance, &count, devices.data());

    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        for (auto pd : devices) g_physDeviceToInstance[pd] = *pInstance;
        g_instanceNextGipa[*pInstance] = gpa;
    }

    fprintf(stderr, "[eq_overlay] hooked vkCreateInstance (%u physical devices)\n", count);
    LoadSavedPositions();
    StartBroadcastClientThread();
    StartPositionIpcThread();
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_CreateDevice(VkPhysicalDevice physicalDevice,
                                                     const VkDeviceCreateInfo* pCreateInfo,
                                                     const VkAllocationCallbacks* pAllocator,
                                                     VkDevice* pDevice) {
    auto* chainInfo = (VkLayerDeviceCreateInfo*)pCreateInfo->pNext;
    while (chainInfo && !(chainInfo->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO &&
                           chainInfo->function == VK_LAYER_LINK_INFO)) {
        chainInfo = (VkLayerDeviceCreateInfo*)chainInfo->pNext;
    }
    if (!chainInfo) return VK_ERROR_INITIALIZATION_FAILED;

    PFN_vkGetInstanceProcAddr gipa = chainInfo->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = chainInfo->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    chainInfo->u.pLayerInfo = chainInfo->u.pLayerInfo->pNext;

    auto createFunc = (PFN_vkCreateDevice)gipa(nullptr, "vkCreateDevice");
    VkResult result = createFunc(physicalDevice, pCreateInfo, pAllocator, pDevice);
    if (result != VK_SUCCESS) return result;

    DeviceData dd;
    dd.device = *pDevice;
    dd.physicalDevice = physicalDevice;
    dd.nextGetDeviceProcAddr = gdpa;
    dd.nextGetInstanceProcAddr = gipa;
    dd.nextCreateSwapchainKHR = (PFN_vkCreateSwapchainKHR)gdpa(*pDevice, "vkCreateSwapchainKHR");
    dd.nextDestroySwapchainKHR = (PFN_vkDestroySwapchainKHR)gdpa(*pDevice, "vkDestroySwapchainKHR");
    dd.nextQueuePresentKHR = (PFN_vkQueuePresentKHR)gdpa(*pDevice, "vkQueuePresentKHR");

    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        auto it = g_physDeviceToInstance.find(physicalDevice);
        dd.instance = (it != g_physDeviceToInstance.end()) ? it->second : VK_NULL_HANDLE;
    }

    // Resolved via gipa/gdpa (the "next" chain pointers captured above),
    // not global linked symbols - see the dispatch-safety note above
    // InitSwapchainResources. dd.instance must already be set (just above).
    auto nextGetQFP = (PFN_vkGetPhysicalDeviceQueueFamilyProperties)gipa(
        dd.instance, "vkGetPhysicalDeviceQueueFamilyProperties");
    auto nextGetDeviceQueue = (PFN_vkGetDeviceQueue)gdpa(*pDevice, "vkGetDeviceQueue");

    dd.graphicsQueueFamily = FindGraphicsQueueFamily(nextGetQFP, physicalDevice);
    nextGetDeviceQueue(*pDevice, dd.graphicsQueueFamily, 0, &dd.graphicsQueue);

    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        g_deviceMap[*pDevice] = dd;
    }

    fprintf(stderr, "[eq_overlay] hooked vkCreateDevice\n");
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_CreateSwapchainKHR(VkDevice device,
                                                           const VkSwapchainCreateInfoKHR* pCreateInfo,
                                                           const VkAllocationCallbacks* pAllocator,
                                                           VkSwapchainKHR* pSwapchain) {
    DeviceData dd;
    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        dd = g_deviceMap.at(device);
    }

    VkResult result = dd.nextCreateSwapchainKHR(device, pCreateInfo, pAllocator, pSwapchain);
    if (result != VK_SUCCESS) return result;

    SwapchainData sc;
    sc.device = device;
    sc.extent = pCreateInfo->imageExtent;
    sc.format = pCreateInfo->imageFormat;

    std::lock_guard<std::mutex> lock(g_mapMutex);
    g_swapchainMap[*pSwapchain] = std::move(sc);

    fprintf(stderr, "[eq_overlay] hooked vkCreateSwapchainKHR (%ux%u)\n", pCreateInfo->imageExtent.width,
            pCreateInfo->imageExtent.height);
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL Overlay_DestroySwapchainKHR(VkDevice device, VkSwapchainKHR swapchain,
                                                        const VkAllocationCallbacks* pAllocator) {
    DeviceData dd;
    SwapchainData sc;
    bool hadSwapchain = false;
    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        dd = g_deviceMap.at(device);
        auto it = g_swapchainMap.find(swapchain);
        if (it != g_swapchainMap.end()) {
            sc = std::move(it->second);
            hadSwapchain = true;
            g_swapchainMap.erase(it);
        }
    }
    // DestroySwapchainResources (ImGui shutdown + vkDestroy* calls) runs
    // unlocked, for the same reentrancy reason as Overlay_QueuePresentKHR
    // above.
    if (hadSwapchain) DestroySwapchainResources(sc);
    dd.nextDestroySwapchainKHR(device, swapchain, pAllocator);
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR* pPresentInfo) {
    if (pPresentInfo->swapchainCount > 0) {
        VkSwapchainKHR swapchain = pPresentInfo->pSwapchains[0];
        uint32_t imageIndex = pPresentInfo->pImageIndices[0];

        // Look up pointers under the lock, then RELEASE it before doing any
        // rendering work below. std::unordered_map references/pointers stay
        // valid across concurrent insertions (only erasing that exact
        // element invalidates them), so this is safe for the spike's
        // single-thread-presents-this-swapchain usage. Holding the lock
        // through the render call deadlocks: the Vulkan loader's own
        // "unknown extension function" resolution path
        // (loader_gpa_instance_terminator, used e.g. for
        // vkCmdBeginRenderingKHR queries during ImGui_ImplVulkan_Init) can
        // re-enter this layer's own GetDeviceProcAddr export from a
        // different call path while we're still inside the render call -
        // confirmed live via gdb backtrace on a hung vkcube.
        SwapchainData* sc = nullptr;
        DeviceData* dd = nullptr;
        {
            std::lock_guard<std::mutex> lock(g_mapMutex);
            auto scIt = g_swapchainMap.find(swapchain);
            if (scIt != g_swapchainMap.end()) {
                sc = &scIt->second;
                dd = &g_deviceMap.at(sc->device);
            }
        }
        if (sc && dd) {
            if (!sc->imguiBackendInitialized) InitSwapchainResources(*sc, *dd, swapchain);
            DrawOverlayFrame(*sc, *dd, imageIndex);
        }
    }

    DeviceData dd;
    {
        // Any device sharing this queue has the same "next" present pointer
        // in practice (single-device spike scope); look it up by scanning -
        // fine for phase-1 with exactly one device.
        std::lock_guard<std::mutex> lock(g_mapMutex);
        for (auto& [dev, data] : g_deviceMap) {
            if (data.graphicsQueue == queue) {
                dd = data;
                break;
            }
        }
    }
    if (!dd.nextQueuePresentKHR) {
        // queue wasn't our tracked graphics queue (e.g. a dedicated present
        // queue); fall back to any known device's next-pointer - correct
        // for the single-device spike case.
        std::lock_guard<std::mutex> lock(g_mapMutex);
        if (!g_deviceMap.empty()) dd = g_deviceMap.begin()->second;
    }
    return dd.nextQueuePresentKHR(queue, pPresentInfo);
}

// ---- Required layer enumeration exports ---------------------------------

VKAPI_ATTR VkResult VKAPI_CALL Overlay_EnumerateInstanceLayerProperties(uint32_t* pCount,
                                                                         VkLayerProperties* pProps) {
    if (pProps == nullptr) {
        *pCount = 1;
        return VK_SUCCESS;
    }
    if (*pCount < 1) return VK_INCOMPLETE;
    std::strncpy(pProps[0].layerName, kLayerName, VK_MAX_EXTENSION_NAME_SIZE);
    std::strncpy(pProps[0].description, "eq-log-suite alert overlay", VK_MAX_DESCRIPTION_SIZE);
    pProps[0].specVersion = VK_API_VERSION_1_0;
    pProps[0].implementationVersion = 1;
    *pCount = 1;
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_EnumerateInstanceExtensionProperties(const char* pLayerName,
                                                                             uint32_t* pCount, VkExtensionProperties*) {
    if (pLayerName && std::strcmp(pLayerName, kLayerName) == 0) {
        *pCount = 0;
        return VK_SUCCESS;
    }
    return VK_ERROR_LAYER_NOT_PRESENT;
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_EnumerateDeviceLayerProperties(VkPhysicalDevice, uint32_t* pCount,
                                                                       VkLayerProperties* pProps) {
    return Overlay_EnumerateInstanceLayerProperties(pCount, pProps);
}

VKAPI_ATTR VkResult VKAPI_CALL Overlay_EnumerateDeviceExtensionProperties(VkPhysicalDevice physicalDevice,
                                                                           const char* pLayerName, uint32_t* pCount,
                                                                           VkExtensionProperties* pProps) {
    if (pLayerName && std::strcmp(pLayerName, kLayerName) == 0) {
        *pCount = 0;
        return VK_SUCCESS;
    }
    // Unlike vkEnumerateInstance*, this call IS chained through the normal
    // per-instance dispatch (apps resolve it via vkGetInstanceProcAddr with
    // a VkPhysicalDevice, since no VkDevice exists yet at this point) - so
    // the real (pLayerName == nullptr) query must be forwarded to the next
    // link in the chain, not rejected. See the dispatch-safety note above
    // InitSwapchainResources for why this uses the captured next-gipa
    // rather than a globally-linked symbol.
    PFN_vkGetInstanceProcAddr nextGipa;
    VkInstance instance;
    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        instance = g_physDeviceToInstance.at(physicalDevice);
        nextGipa = g_instanceNextGipa.at(instance);
    }
    auto next =
        (PFN_vkEnumerateDeviceExtensionProperties)nextGipa(instance, "vkEnumerateDeviceExtensionProperties");
    return next(physicalDevice, pLayerName, pCount, pProps);
}

// ---- GetProcAddr dispatch -------------------------------------------------

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL Overlay_GetDeviceProcAddr(VkDevice device, const char* name);
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL Overlay_GetInstanceProcAddr(VkInstance instance, const char* name);

#define INTERCEPT(fn)                          \
    if (std::strcmp(name, "vk" #fn) == 0)      \
        return (PFN_vkVoidFunction)&Overlay_##fn;

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL Overlay_GetDeviceProcAddr(VkDevice device, const char* name) {
    INTERCEPT(GetDeviceProcAddr)
    INTERCEPT(CreateSwapchainKHR)
    INTERCEPT(DestroySwapchainKHR)
    INTERCEPT(QueuePresentKHR)
    INTERCEPT(EnumerateDeviceLayerProperties)
    INTERCEPT(EnumerateDeviceExtensionProperties)

    PFN_vkGetDeviceProcAddr next;
    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        next = g_deviceMap.at(device).nextGetDeviceProcAddr;
    }
    return next(device, name);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL Overlay_GetInstanceProcAddr(VkInstance instance, const char* name) {
    INTERCEPT(GetInstanceProcAddr)
    INTERCEPT(GetDeviceProcAddr)
    INTERCEPT(CreateInstance)
    INTERCEPT(CreateDevice)
    INTERCEPT(CreateSwapchainKHR)
    INTERCEPT(DestroySwapchainKHR)
    INTERCEPT(QueuePresentKHR)
    INTERCEPT(EnumerateInstanceLayerProperties)
    INTERCEPT(EnumerateInstanceExtensionProperties)
    INTERCEPT(EnumerateDeviceLayerProperties)
    INTERCEPT(EnumerateDeviceExtensionProperties)

    PFN_vkGetInstanceProcAddr next;
    {
        std::lock_guard<std::mutex> lock(g_mapMutex);
        auto it = g_instanceNextGipa.find(instance);
        next = (it != g_instanceNextGipa.end()) ? it->second : vkGetInstanceProcAddr;
    }
    return next(instance, name);
}

#undef INTERCEPT

} // namespace

// ---- Loader-visible exports ----------------------------------------------
// Names required by the Vulkan loader for a layer library, per the
// VK_LAYER_EQLOG_overlay.json manifest's "functions" block.

extern "C" {

VKAPI_ATTR VkResult VKAPI_CALL EQLOG_Overlay_EnumerateInstanceLayerProperties(uint32_t* pCount,
                                                                               VkLayerProperties* pProps) {
    return Overlay_EnumerateInstanceLayerProperties(pCount, pProps);
}

VKAPI_ATTR VkResult VKAPI_CALL EQLOG_Overlay_EnumerateInstanceExtensionProperties(const char* pLayerName,
                                                                                   uint32_t* pCount,
                                                                                   VkExtensionProperties* pProps) {
    return Overlay_EnumerateInstanceExtensionProperties(pLayerName, pCount, pProps);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL EQLOG_Overlay_GetInstanceProcAddr(VkInstance instance, const char* name) {
    return Overlay_GetInstanceProcAddr(instance, name);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL EQLOG_Overlay_GetDeviceProcAddr(VkDevice device, const char* name) {
    return Overlay_GetDeviceProcAddr(device, name);
}

} // extern "C"
