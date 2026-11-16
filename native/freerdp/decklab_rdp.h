/* A small C ABI around FreeRDP. No FreeRDP structures cross this boundary. */
#ifndef DECKLAB_RDP_H
#define DECKLAB_RDP_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(DECKLAB_RDP_BUILD)
#define DL_RDP_API __declspec(dllexport)
#else
#define DL_RDP_API __declspec(dllimport)
#endif
#else
#define DL_RDP_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define DL_RDP_ABI_VERSION 1
#define DL_RDP_MAX_TEXT_UNITS 2048
#define DL_RDP_MAX_CLIPBOARD_UNITS 65536

typedef struct dl_rdp dl_rdp;

typedef struct {
    uint32_t size;
    uint32_t abi_version;
    const char* hostname;
    const char* username;
    const char* password;
    const char* domain;
    const char* certificate_sha256;
    uint32_t port;
    uint32_t width;
    uint32_t height;
} dl_rdp_options;

/* The framebuffer belongs to FreeRDP and is valid only during this callback.
 * gdi_init uses FreeRDP PIXEL_FORMAT_BGRX32, whose memory is B,G,R,X. */
typedef void (*dl_rdp_frame_cb)(void* user, const uint8_t* pixels, uint32_t width,
    uint32_t height, uint32_t stride, uint32_t x, uint32_t y, uint32_t dirty_width,
    uint32_t dirty_height, uint64_t generation);

/* A terminal notification only; never includes credentials or raw errors. */
typedef void (*dl_rdp_closed_cb)(void* user);

DL_RDP_API uint32_t dl_rdp_abi_version(void);
DL_RDP_API dl_rdp* dl_rdp_new(const dl_rdp_options* options, dl_rdp_frame_cb frame,
    dl_rdp_closed_cb closed, void* user);
/* Returns 1 on success and 0 on failure/timeout. A failed command is never retried. */
DL_RDP_API int dl_rdp_start(dl_rdp* session, uint32_t timeout_ms);
/* Static FreeRDP reason and certificate decision; never credentials or stderr.
 * Returns the native error code and writes a bounded, NUL-terminated reason. */
DL_RDP_API uint32_t dl_rdp_error(dl_rdp* session, char* reason, uint32_t capacity);
/* buttons: left=1, middle=2, right=4; positive wheel steps scroll down. */
DL_RDP_API int dl_rdp_pointer(dl_rdp* session, uint32_t x, uint32_t y,
    uint32_t buttons, int32_t wheel_steps, uint32_t timeout_ms);
/* unicode_key=0: FreeRDP scan code (low byte + 0x100 extended); otherwise UTF-16 unit. */
DL_RDP_API int dl_rdp_key(dl_rdp* session, uint32_t value, uint32_t unicode_key,
    uint32_t down, uint32_t timeout_ms);
/* Uses WinPR's native keyboard data. Zero means an unknown virtual-key name. */
DL_RDP_API uint32_t dl_rdp_keycode(const char* virtual_key_name);
DL_RDP_API int dl_rdp_text(dl_rdp* session, const uint16_t* text, uint32_t units,
    uint32_t timeout_ms);
DL_RDP_API int dl_rdp_clipboard_write(dl_rdp* session, const uint16_t* text,
    uint32_t units, uint32_t timeout_ms);
/* caller capacity is UTF-16 units; no terminating NUL is included in written. */
DL_RDP_API int dl_rdp_clipboard_read(dl_rdp* session, uint16_t* text,
    uint32_t capacity, uint32_t* written, uint32_t* available, uint32_t timeout_ms);
DL_RDP_API int dl_rdp_release_all(dl_rdp* session, uint32_t timeout_ms);
/* Stops IO, closes only this transport and joins callbacks before freeing. Never logs off. */
DL_RDP_API void dl_rdp_free(dl_rdp* session);

#ifdef __cplusplus
}
#endif
#endif
