cmake_minimum_required(VERSION 3.13)
project(decklab_freerdp LANGUAGES C)

add_library(decklab-freerdp SHARED decklab_rdp.c)
target_compile_features(decklab-freerdp PRIVATE c_std_11)
target_compile_definitions(decklab-freerdp PRIVATE DECKLAB_RDP_BUILD)
set_target_properties(decklab-freerdp PROPERTIES C_VISIBILITY_PRESET hidden)

# An installed private SDK is the release path. The explicit build-tree option
# permits compile-only development against an already prepared upstream build.
if(DECKLAB_FREERDP_SOURCE AND DECKLAB_FREERDP_BUILD)
  target_include_directories(decklab-freerdp PRIVATE
    "${DECKLAB_FREERDP_SOURCE}/include" "${DECKLAB_FREERDP_BUILD}/include"
    "${DECKLAB_FREERDP_SOURCE}/winpr/include" "${DECKLAB_FREERDP_BUILD}/winpr/include")
  find_library(DECKLAB_FREERDP_CLIENT NAMES freerdp-client3
    PATHS "${DECKLAB_FREERDP_BUILD}/client/common" NO_DEFAULT_PATH REQUIRED)
  find_library(DECKLAB_FREERDP_CORE NAMES freerdp3
    PATHS "${DECKLAB_FREERDP_BUILD}/libfreerdp" NO_DEFAULT_PATH REQUIRED)
  find_library(DECKLAB_WINPR NAMES winpr3
    PATHS "${DECKLAB_FREERDP_BUILD}/winpr/libwinpr" NO_DEFAULT_PATH REQUIRED)
  target_link_libraries(decklab-freerdp PRIVATE
    "${DECKLAB_FREERDP_CLIENT}" "${DECKLAB_FREERDP_CORE}" "${DECKLAB_WINPR}")
else()
  find_package(WinPR 3.33.1 EXACT CONFIG REQUIRED)
  find_package(FreeRDP 3.33.1 EXACT CONFIG REQUIRED)
  find_package(FreeRDP-Client 3.33.1 EXACT CONFIG REQUIRED)
  target_link_libraries(decklab-freerdp PRIVATE freerdp-client freerdp winpr)
endif()

if(MSVC)
  target_compile_options(decklab-freerdp PRIVATE /W4)
else()
  target_compile_options(decklab-freerdp PRIVATE -Wall -Wextra -Werror)
endif()
if(UNIX)
  set_target_properties(decklab-freerdp PROPERTIES
    INSTALL_RPATH "$ORIGIN;$ORIGIN/lib" BUILD_WITH_INSTALL_RPATH TRUE)
endif()
if(WIN32)
  set_target_properties(decklab-freerdp PROPERTIES PREFIX "")
  target_link_libraries(decklab-freerdp PRIVATE crypt32)
endif()
install(TARGETS decklab-freerdp RUNTIME DESTINATION . LIBRARY DESTINATION .)

option(DECKLAB_RDP_BUILD_TESTS "Build offline native adapter regressions" OFF)
if(DECKLAB_RDP_BUILD_TESTS)
  if(NOT EXISTS "${DECKLAB_FREERDP_TEST_SOURCE}/libfreerdp/cache/cache.h")
    message(FATAL_ERROR "DECKLAB_FREERDP_TEST_SOURCE must name the original FreeRDP 3.33.1 source for offline cache setup")
  endif()
  # Include the production C so its actual PostConnect pixel format is tested.
  # This test executable is deliberately not an installed runtime artifact.
  add_executable(decklab-freerdp-color-test test_color.c)
  add_executable(decklab-freerdp-runtime-test test_runtime.c)
  target_include_directories(decklab-freerdp-color-test PRIVATE
    "${DECKLAB_FREERDP_TEST_SOURCE}/libfreerdp")
  get_target_property(DECKLAB_RDP_TEST_LIBS decklab-freerdp LINK_LIBRARIES)
  get_target_property(DECKLAB_RDP_TEST_INCLUDES decklab-freerdp INCLUDE_DIRECTORIES)
  foreach(DECKLAB_RDP_TEST_TARGET IN ITEMS decklab-freerdp-color-test decklab-freerdp-runtime-test)
    target_compile_features(${DECKLAB_RDP_TEST_TARGET} PRIVATE c_std_11)
    target_compile_definitions(${DECKLAB_RDP_TEST_TARGET} PRIVATE DECKLAB_RDP_BUILD)
    if(MINGW)
      target_compile_definitions(${DECKLAB_RDP_TEST_TARGET} PRIVATE __USE_MINGW_ANSI_STDIO=1)
    endif()
    target_link_libraries(${DECKLAB_RDP_TEST_TARGET} PRIVATE ${DECKLAB_RDP_TEST_LIBS})
    if(DECKLAB_RDP_TEST_INCLUDES)
      target_include_directories(${DECKLAB_RDP_TEST_TARGET} PRIVATE ${DECKLAB_RDP_TEST_INCLUDES})
    endif()
    if(MSVC)
      target_compile_options(${DECKLAB_RDP_TEST_TARGET} PRIVATE /W4)
    else()
      target_compile_options(${DECKLAB_RDP_TEST_TARGET} PRIVATE -Wall -Wextra -Werror)
    endif()
  endforeach()
  enable_testing()
  add_test(NAME decklab-freerdp-colors COMMAND decklab-freerdp-color-test
    "${CMAKE_CURRENT_BINARY_DIR}/gdi-colors.raw")
  add_test(NAME decklab-freerdp-runtime COMMAND decklab-freerdp-runtime-test)
endif()
