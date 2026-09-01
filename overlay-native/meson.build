project('eq_overlay', 'cpp',
  version : '0.1.0-spike',
  default_options : ['cpp_std=c++17', 'warning_level=2'])

vulkan_dep = dependency('vulkan')
threads_dep = dependency('threads')
# wrapdb's imgui.wrap auto-detects the 'vulkan' dependency and compiles
# backends/imgui_impl_vulkan.cpp into imgui_dep itself (see
# subprojects/imgui-1.92.8/meson.build) - no need to grab that file
# ourselves (meson's subproject sandbox forbids reaching into a nested
# subproject's sources from the parent project anyway).
# IMGUI_IMPL_VULKAN_NO_PROTOTYPES: without this, imgui_impl_vulkan.cpp calls
# vk* functions via normal global linkage against libvulkan.so - which
# bypasses this layer's captured "next"-chain dispatch pointers entirely.
# Confirmed via live debugging (vkcube, phase-1 spike) that the globally
# linked vkGetPhysicalDeviceProperties aborts ("Invalid physicalDevice")
# for a handle this same layer's own next-chain-resolved call accepts
# fine - so ImGui's internal Vulkan calls must go through our own loader
# function (installed via ImGui_ImplVulkan_LoadFunctions in overlay_layer.cpp)
# instead of static linkage.
#
# dependency(..., default_options: ['cpp_args=...']) does NOT propagate a
# built-in compiler-args option into a fallback subproject (confirmed via
# compile_commands.json - the flag never appeared); add_global_arguments()
# called here, before any subdir()/dependency() triggers the compiler,
# does reach the imgui subproject's own build too.
add_global_arguments('-DIMGUI_IMPL_VULKAN_NO_PROTOTYPES', language : 'cpp')

# Static, not shared: the layer .so gets deployed into
# ~/.local/share/vulkan/implicit_layer.d/ on its own (matching where
# MangoHud's own manifest+.so live) - a separate libimgui.so dependency
# sitting next to it wouldn't be on the dynamic linker's search path there,
# and the Vulkan loader confirmed this live ("libimgui.so: cannot open
# shared object file") the first time this was deployed and actually
# requested by a real vkCreateInstance call, despite loading fine in every
# earlier vkcube test run directly from the build tree.
imgui_dep = dependency('imgui', fallback : ['imgui', 'imgui_dep'],
                        default_options : ['default_library=static'])

subdir('layer')
subdir('companion')
