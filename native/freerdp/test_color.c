/* SPDX-License-Identifier: Apache-2.0
 * No network: production PostConnect/GDI/EndPaint and upstream GFX callbacks.
 */
#include <stdio.h>
#include <string.h>
#include <freerdp/codec/color.h>
#include <freerdp/gdi/gfx.h>
#include <freerdp/gdi/region.h>
#include <cache/cache.h>

#include "decklab_rdp.c"

#define CHECK(test) do { if (!(test)) { \
    fprintf(stderr, "GDI check failed at line %u: %s\n", (unsigned)__LINE__, #test); \
    return FALSE; } } while (0)

typedef struct {
    BYTE pixels[256];
    uint32_t width, height, stride, x, y, dirty_width, dirty_height;
    uint64_t generation;
    unsigned calls;
    BOOL invalid;
    CRITICAL_SECTION lock;
    HANDLE entered;
    LONG active, peak, delay;
} color_capture;

typedef struct { void* surface; } surface_store;
typedef struct { unsigned frames, generation, peak; } gfx_result;

static void capture_frame(void* user, const uint8_t* pixels, uint32_t width,
    uint32_t height, uint32_t stride, uint32_t x, uint32_t y, uint32_t dirty_width,
    uint32_t dirty_height, uint64_t generation)
{
    color_capture* captured = user;
    LONG active = InterlockedIncrement(&captured->active);
    LONG peak;
    do { peak = InterlockedCompareExchange(&captured->peak, 0, 0); } while (active > peak &&
        InterlockedCompareExchange(&captured->peak, active, peak) != peak);
    if (InterlockedExchange(&captured->delay, 0)) {
        SetEvent(captured->entered);
        Sleep(60); /* Let the second drawing thread compete for the update lock. */
    }
    EnterCriticalSection(&captured->lock);
    if (!pixels || !width || !height || stride < width * 4 ||
        (uint64_t)stride * height > sizeof(captured->pixels) ||
        x >= width || y >= height || !dirty_width || !dirty_height ||
        dirty_width > width - x || dirty_height > height - y) {
        captured->invalid = TRUE;
    } else {
        captured->width = width;
        captured->height = height;
        captured->stride = stride;
        captured->x = x;
        captured->y = y;
        captured->dirty_width = dirty_width;
        captured->dirty_height = dirty_height;
        captured->generation = generation;
        captured->calls++;
        memcpy(captured->pixels, pixels, (size_t)stride * height);
    }
    LeaveCriticalSection(&captured->lock);
    InterlockedDecrement(&captured->active);
}

static void free_session(dl_rdp* session)
{
    if (!session) return;
    gdi_free(session->context->instance);
    free(session->context->cache);
    session->context->cache = NULL;
    dl_rdp_free(session);
}

static dl_rdp* create_session(color_capture* captured, uint32_t width, uint32_t height,
    int* error)
{
    dl_rdp_options options = { 0 };
    options.size = sizeof(options);
    options.abi_version = DL_RDP_ABI_VERSION;
    options.hostname = "offline.decklab.invalid";
    options.port = 3389;
    options.width = width;
    options.height = height;
    dl_rdp* session = dl_rdp_new(&options, capture_frame, NULL, captured);
    if (!session) { *error = 10; return NULL; }
    /* A real connection creates its cache during capability activation. The
     * private constructor is not exported, so this software-drawing fixture
     * uses the original cache layout without decoding caches. No transport is
     * started. PostConnect restores decoding before the real GFX tests below. */
    session->context->cache = calloc(1, sizeof(rdpCache));
    if (!session->context->cache || !freerdp_settings_set_bool(session->context->settings,
        FreeRDP_DeactivateClientDecoding, TRUE)) {
        *error = 12;
        free_session(session);
        return NULL;
    }
    if (!dl_post_connect(session->context->instance)) {
        *error = 11;
        free_session(session);
        return NULL;
    }
    return session;
}

static BOOL pixel_is(const color_capture* captured, uint32_t x, uint32_t y,
    BYTE red, BYTE green, BYTE blue)
{
    if (captured->invalid || x >= captured->width || y >= captured->height) return FALSE;
    const BYTE* pixel = captured->pixels + y * captured->stride + x * 4;
    return pixel[0] == blue && pixel[1] == green && pixel[2] == red;
}

static BOOL all_pixels_are(const color_capture* captured, BYTE red, BYTE green, BYTE blue)
{
    for (uint32_t y = 0; y < captured->height; y++)
        for (uint32_t x = 0; x < captured->width; x++)
            if (!pixel_is(captured, x, y, red, green, blue)) return FALSE;
    return !captured->invalid;
}

/* Storage only: drawing, dirty tracking, update locking and GDI buffer copies
 * are performed by the original FreeRDP GFX handlers installed below. */
static void* get_surface(RdpgfxClientContext* gfx, UINT16 id)
{
    return id == 1 ? ((surface_store*)gfx->handle)->surface : NULL;
}

static UINT set_surface(RdpgfxClientContext* gfx, UINT16 id, void* data)
{
    if (id != 1) return ERROR_INVALID_PARAMETER;
    ((surface_store*)gfx->handle)->surface = data;
    return CHANNEL_RC_OK;
}

static UINT get_surface_ids(RdpgfxClientContext* gfx, UINT16** ids, UINT16* count)
{
    *count = ((surface_store*)gfx->handle)->surface ? 1 : 0;
    *ids = *count ? calloc(1, sizeof(UINT16)) : NULL;
    if (*count && !*ids) return CHANNEL_RC_NO_MEMORY;
    if (*count) **ids = 1;
    return CHANNEL_RC_OK;
}

static BOOL create_surface(RdpgfxClientContext* gfx, UINT16 width, UINT16 height)
{
    RDPGFX_CREATE_SURFACE_PDU create = { .surfaceId = 1, .width = width,
        .height = height, .pixelFormat = GFX_PIXEL_FORMAT_XRGB_8888 };
    RDPGFX_MAP_SURFACE_TO_OUTPUT_PDU map = { .surfaceId = 1 };
    CHECK(gfx->CreateSurface(gfx, &create) == CHANNEL_RC_OK);
    CHECK(gfx->MapSurfaceToOutput(gfx, &map) == CHANNEL_RC_OK);
    return TRUE;
}

static BOOL gfx_fill(RdpgfxClientContext* gfx, UINT16 x, UINT16 y, UINT16 width,
    UINT16 height, BYTE red, BYTE green, BYTE blue)
{
    RECTANGLE_16 rect = { x, y, x + width, y + height };
    RDPGFX_SOLID_FILL_PDU fill = { .surfaceId = 1,
        .fillPixel = { .R = red, .G = green, .B = blue, .XA = 255 },
        .fillRectCount = 1, .fillRects = &rect };
    return gfx->SolidFill(gfx, &fill) == CHANNEL_RC_OK;
}

static BOOL gfx_paint(RdpgfxClientContext* gfx, UINT16 x, UINT16 y, UINT16 width,
    UINT16 height, BYTE red, BYTE green, BYTE blue, UINT32 frame)
{
    RDPGFX_START_FRAME_PDU start = { .frameId = frame };
    RDPGFX_END_FRAME_PDU end = { .frameId = frame };
    CHECK(gfx->StartFrame(gfx, &start) == CHANNEL_RC_OK);
    CHECK(gfx_fill(gfx, x, y, width, height, red, green, blue));
    CHECK(gfx->EndFrame(gfx, &end) == CHANNEL_RC_OK);
    return TRUE;
}

typedef struct { RdpgfxClientContext* gfx; BYTE red, green; BOOL success; } drawing_thread;
static DWORD WINAPI threaded_fill(void* data)
{
    drawing_thread* drawing = data;
    drawing->success = gfx_fill(drawing->gfx, 0, 0, 4, 4, drawing->red, drawing->green, 0);
    return 0;
}

static BOOL check_drawing_threads(RdpgfxClientContext* gfx, color_capture* captured)
{
    drawing_thread first = { .gfx = gfx, .red = 255 }, second = { .gfx = gfx, .green = 255 };
    captured->entered = CreateEvent(NULL, TRUE, FALSE, NULL);
    CHECK(captured->entered);
    captured->delay = 1;
    HANDLE a = CreateThread(NULL, 0, threaded_fill, &first, 0, NULL);
    CHECK(a);
    BOOL entered = WaitForSingleObject(captured->entered, 5000) == WAIT_OBJECT_0;
    HANDLE b = entered ? CreateThread(NULL, 0, threaded_fill, &second, 0, NULL) : NULL;
    /* Never free the context while a failed or hung test still has a callback. */
    if (WaitForSingleObject(a, 5000) != WAIT_OBJECT_0 ||
        (b && WaitForSingleObject(b, 5000) != WAIT_OBJECT_0)) exit(3);
    CloseHandle(a);
    if (b) CloseHandle(b);
    CHECK(entered && b && first.success && second.success);
    CHECK(captured->peak == 1 && captured->active == 0);
    CHECK(all_pixels_are(captured, 0, 255, 0));
    return TRUE;
}

static BOOL check_gfx_updates(dl_rdp* session, RdpgfxClientContext* gfx,
    color_capture* captured)
{
    CHECK(!freerdp_settings_get_bool(session->context->settings, FreeRDP_AsyncUpdate));
    CHECK(create_surface(gfx, 4, 4));
    CHECK(gfx_paint(gfx, 0, 0, 4, 4, 0, 0, 0, 1));
    CHECK(captured->calls == 1 && captured->generation == 1);
    CHECK(gfx_paint(gfx, 1, 1, 1, 1, 255, 0, 0, 2));
    CHECK(captured->calls == 2 && captured->x == 1 && captured->y == 1 &&
        captured->dirty_width == 1 && captured->dirty_height == 1);
    CHECK(pixel_is(captured, 1, 1, 255, 0, 0) && pixel_is(captured, 0, 0, 0, 0, 0));
    RDPGFX_START_FRAME_PDU start = { .frameId = 3 };
    RDPGFX_END_FRAME_PDU end = { .frameId = 3 };
    CHECK(gfx->StartFrame(gfx, &start) == CHANNEL_RC_OK);
    CHECK(gfx->EndFrame(gfx, &end) == CHANNEL_RC_OK);
    CHECK(captured->calls == 2); /* An empty frame must not reuse a prior dirty region. */
    CHECK(gfx_paint(gfx, 2, 2, 1, 1, 0, 255, 0, 4));
    CHECK(captured->calls == 3 && captured->x == 2 && captured->y == 2);
    CHECK(pixel_is(captured, 1, 1, 255, 0, 0) && pixel_is(captured, 2, 2, 0, 255, 0));
    CHECK(gfx_paint(gfx, 1, 1, 1, 1, 0, 0, 255, 5));
    CHECK(pixel_is(captured, 1, 1, 0, 0, 255) && pixel_is(captured, 2, 2, 0, 255, 0));
    for (unsigned n = 0; n < 200; n++) {
        CHECK(gfx_paint(gfx, 0, 0, 1, 1, (BYTE)n, (BYTE)(n + 1), (BYTE)(n + 2), n + 6));
        CHECK(pixel_is(captured, 0, 0, (BYTE)n, (BYTE)(n + 1), (BYTE)(n + 2)));
        CHECK(pixel_is(captured, 1, 1, 0, 0, 255));
    }
    HGDI_WND window = session->context->gdi->primary->hdc->hwnd;
    CHECK(window->ninvalid == 1 && window->count == 32); /* Original wrapper resets the list. */
    CHECK(check_drawing_threads(gfx, captured));
    unsigned before_resize = captured->calls;
    RDPGFX_RESET_GRAPHICS_PDU reset = { .width = 5, .height = 3 };
    CHECK(gfx->ResetGraphics(gfx, &reset) == CHANNEL_RC_OK);
    CHECK(captured->calls == before_resize + 1); /* Upstream resize flushes the old generation. */
    CHECK(captured->width == 4 && captured->height == 4 && captured->generation == 1);
    RDPGFX_DELETE_SURFACE_PDU deletion = { .surfaceId = 1 };
    CHECK(gfx->DeleteSurface(gfx, &deletion) == CHANNEL_RC_OK);
    CHECK(create_surface(gfx, 5, 3));
    CHECK(gfx_paint(gfx, 0, 0, 5, 3, 0, 255, 255, 206));
    CHECK(captured->width == 5 && captured->height == 3 && captured->generation == 2);
    CHECK(all_pixels_are(captured, 0, 255, 255));
    CHECK(gfx->DeleteSurface(gfx, &deletion) == CHANNEL_RC_OK);
    return TRUE;
}

static BOOL check_gfx_frames(gfx_result* result)
{
    color_capture captured = { 0 };
    InitializeCriticalSection(&captured.lock);
    int error = 0;
    dl_rdp* session = create_session(&captured, 4, 4, &error);
    surface_store store = { 0 };
    RdpgfxClientContext gfx = { .handle = &store, .GetSurfaceData = get_surface,
        .SetSurfaceData = set_surface, .GetSurfaceIds = get_surface_ids };
    BOOL initialized = session && gdi_graphics_pipeline_init(session->context->gdi, &gfx);
    BOOL correct = initialized && check_gfx_updates(session, &gfx, &captured);
    if (initialized) {
        if (store.surface) {
            RDPGFX_DELETE_SURFACE_PDU deletion = { .surfaceId = 1 };
            if (gfx.DeleteSurface(&gfx, &deletion) != CHANNEL_RC_OK) correct = FALSE;
        }
        gdi_graphics_pipeline_uninit(session->context->gdi, &gfx);
    }
    if (correct) {
        gdi_free(session->context->instance);
        correct = freerdp_settings_set_bool(session->context->settings,
            FreeRDP_DeactivateClientDecoding, TRUE) && dl_post_connect(session->context->instance);
        initialized = correct && gdi_graphics_pipeline_init(session->context->gdi, &gfx);
        correct = initialized && create_surface(&gfx, 5, 3) &&
            gfx_paint(&gfx, 0, 0, 5, 3, 255, 0, 255, 207) && captured.generation == 3 &&
            all_pixels_are(&captured, 255, 0, 255);
        if (initialized) {
            if (store.surface) {
                RDPGFX_DELETE_SURFACE_PDU deletion = { .surfaceId = 1 };
                if (gfx.DeleteSurface(&gfx, &deletion) != CHANNEL_RC_OK) correct = FALSE;
            }
            gdi_graphics_pipeline_uninit(session->context->gdi, &gfx);
        }
    }
    result->frames = captured.calls;
    result->generation = (unsigned)captured.generation;
    result->peak = (unsigned)captured.peak;
    free_session(session);
    if (captured.entered) CloseHandle(captured.entered);
    DeleteCriticalSection(&captured.lock);
    return correct;
}

int main(int argc, char** argv)
{
    const BYTE rgb[3][3] = { { 255, 0, 0 }, { 0, 255, 0 }, { 0, 0, 255 } };
    color_capture captured = { 0 };
    InitializeCriticalSection(&captured.lock);
    int error = 0;
    dl_rdp* session = create_session(&captured, 3, 3, &error);
    if (!session) { DeleteCriticalSection(&captured.lock); return error; }
    rdpGdi* gdi = session->context->gdi;
    const UINT32 format = gdi->dstFormat;
    BOOL painted = TRUE;
    for (UINT32 x = 0; x < 3; x++) {
        UINT32 color = FreeRDPGetColor(gdi->dstFormat, rgb[x][0], rgb[x][1], rgb[x][2], 255);
        if (!freerdp_image_fill(gdi->primary_buffer, gdi->dstFormat, gdi->stride,
            x, 0, 1, 3, color)) painted = FALSE;
    }
    if (!gdi_InvalidateRegion(gdi->primary->hdc, 0, 0, 3, 3)) painted = FALSE;
    if (!dl_end_paint(session->context)) painted = FALSE;
    BOOL correct = painted && !captured.invalid && captured.calls == 1 && captured.generation == 1 &&
        captured.width == 3 && captured.height == 3 && !captured.x && !captured.y &&
        captured.dirty_width == 3 && captured.dirty_height == 3;
    if (correct) for (uint32_t y = 0; y < 3; y++) for (uint32_t x = 0; x < 3; x++)
        if (!pixel_is(&captured, x, y, rgb[x][0], rgb[x][1], rgb[x][2])) correct = FALSE;
    if (argc > 1 && captured.calls == 1 && !captured.invalid) {
        FILE* output = fopen(argv[1], "wb");
        if (!output || fwrite(captured.pixels, captured.stride, captured.height, output) != captured.height)
            correct = FALSE;
        if (output && fclose(output) != 0) correct = FALSE;
    }
    free_session(session);
    gfx_result gfx = { 0 };
    if (correct) correct = check_gfx_frames(&gfx);
    printf("{\"passed\":%s,\"format\":%" PRIu32 ",\"width\":%" PRIu32
        ",\"height\":%" PRIu32 ",\"stride\":%" PRIu32 ",\"generation\":%" PRIu32
        ",\"first_row\":[", correct ? "true" : "false", format, captured.width,
        captured.height, captured.stride, (uint32_t)captured.generation);
    for (unsigned i = 0; i < 12; i++) printf("%s%u", i ? "," : "", captured.pixels[i]);
    printf("],\"gfx_frames\":%u,\"gfx_generation\":%u,\"gfx_callback_peak\":%u}\n",
        gfx.frames, gfx.generation, gfx.peak);
    DeleteCriticalSection(&captured.lock);
    return correct ? 0 : 3;
}
