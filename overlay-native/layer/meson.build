eq_overlay_layer = shared_library('eq_overlay_layer',
  'overlay_layer.cpp',
  'overlay_ipc.cpp',
  dependencies : [vulkan_dep, threads_dep, imgui_dep],
  include_directories : include_directories('../common'),
  cpp_args : ['-DVK_USE_PLATFORM_XLIB_KHR', '-DVK_USE_PLATFORM_WAYLAND_KHR'],
  install : false,
)

# Copy the manifest next to the built .so so VK_LAYER_PATH can point at one
# directory for both, matching the manifest's relative library_path.
configure_file(
  input : 'VkLayer_EQLOG_overlay.json',
  output : 'VkLayer_EQLOG_overlay.json',
  copy : true,
)
