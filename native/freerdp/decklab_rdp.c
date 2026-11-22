/* SPDX-License-Identifier: Apache-2.0
 * Headless FreeRDP adapter. The upstream library owns all RDP protocol state.
 */
#include "decklab_rdp.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <freerdp/client.h>
#include <freerdp/client/channels.h>
#include <freerdp/client/cliprdr.h>
#include <freerdp/crypto/certificate.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/input.h>
#include <freerdp/settings.h>
#include <winpr/clipboard.h>
#include <winpr/input.h>
#include <winpr/interlocked.h>
#include <winpr/synch.h>
#include <winpr/sysinfo.h>
#include <winpr/wlog.h>
#ifdef _WIN32
#include <wincrypt.h>
#endif

#define DL_QUEUE_CAPACITY 32
#define DL_MAX_PIXELS (32u * 1024u * 1024u)
#define DL_MAX_TIMEOUT 25000u

typedef enum {
    DL_POINTER, DL_KEY, DL_TEXT, DL_CLIP_WRITE, DL_CLIP_READ, DL_RELEASE
} dl_command_type;

typedef struct dl_command {
    dl_command_type type;
    LONG references;
    LONG completed;
    HANDLE done;
    int result;
    uint32_t x, y, buttons, value, unicode_key, down;
    int32_t wheel_steps;
    uint16_t* text;
    uint32_t units, capacity, available;
    BOOL clipboard_sent;
} dl_command;

typedef struct {
    rdpClientContext common;
    struct dl_rdp* owner;
} dl_context;

struct dl_rdp {
    rdpContext* context;
    HANDLE thread, stop, ready, queued;
    CRITICAL_SECTION lock;
    BOOL connected, fenced;
    uint32_t last_error, certificate_error;
    dl_command* queue[DL_QUEUE_CAPACITY];
    size_t head, count;
    dl_command* clipboard_write;
    dl_command* clipboard_read;
    CliprdrClientContext* clipboard;
    BOOL clipboard_ready, remote_text, local_owner;
    uint16_t* local_text;
    uint32_t local_units;
    BYTE held_scancodes[512];
    BYTE held_unicode[65536];
    uint32_t buttons, x, y;
    uint64_t generation;
    char* hostname;
    char* pin;
    BOOL accept_certificate;
    uint32_t port;
    dl_rdp_frame_cb frame;
    dl_rdp_closed_cb closed;
    void* user;
};

static dl_rdp* dl_owner(rdpContext* context)
{
    return context ? ((dl_context*)context)->owner : NULL;
}

static BOOL dl_stopped(dl_rdp* session)
{
    return session->stop && WaitForSingleObject(session->stop, 0) == WAIT_OBJECT_0;
}

static BOOL dl_utf16(const uint16_t* text, uint32_t count, uint32_t limit)
{
    if (count > limit || (count && !text)) return FALSE;
    for (uint32_t i = 0; i < count; i++) {
        uint16_t value = text[i];
        if (!value) return FALSE;
        if (value >= 0xd800 && value <= 0xdbff) {
            if (++i >= count || text[i] < 0xdc00 || text[i] > 0xdfff) return FALSE;
        } else if (value >= 0xdc00 && value <= 0xdfff) return FALSE;
    }
    return TRUE;
}

static dl_command* dl_command_new(dl_command_type type)
{
    dl_command* command = calloc(1, sizeof(*command));
    if (!command) return NULL;
    command->type = type;
    command->references = 1;
    command->done = CreateEvent(NULL, TRUE, FALSE, NULL);
    if (!command->done) { free(command); return NULL; }
    return command;
}

static void dl_command_unref(dl_command* command)
{
    if (command && InterlockedDecrement(&command->references) == 0) {
        if (command->text) {
            SecureZeroMemory(command->text, ((size_t)command->capacity + 1) * sizeof(uint16_t));
            free(command->text);
        }
        CloseHandle(command->done);
        free(command);
    }
}

static BOOL dl_command_text(dl_command* command, const uint16_t* text, uint32_t units,
    uint32_t capacity)
{
    if (capacity > DL_RDP_MAX_CLIPBOARD_UNITS || units > capacity) return FALSE;
    command->text = calloc((size_t)capacity + 1, sizeof(uint16_t));
    if (!command->text) return FALSE;
    command->capacity = capacity;
    command->units = units;
    if (units) memcpy(command->text, text, (size_t)units * sizeof(uint16_t));
    return TRUE;
}

static void dl_complete(dl_command* command, int result)
{
    if (InterlockedCompareExchange(&command->completed, 1, 0) == 0) {
        command->result = result;
        SetEvent(command->done);
    }
}

static void dl_fence(dl_rdp* session)
{
    EnterCriticalSection(&session->lock);
    session->fenced = TRUE;
    while (session->count) {
        dl_command* command = session->queue[session->head];
        session->head = (session->head + 1) % DL_QUEUE_CAPACITY;
        session->count--;
        dl_complete(command, 0);
        dl_command_unref(command);
    }
    dl_command* pending[] = { session->clipboard_write, session->clipboard_read };
    session->clipboard_write = session->clipboard_read = NULL;
    for (size_t i = 0; i < 2; i++) {
        if (pending[i]) { dl_complete(pending[i], 0); dl_command_unref(pending[i]); }
    }
    SetEvent(session->stop);
    LeaveCriticalSection(&session->lock);
    if (session->context) freerdp_abort_connect_context(session->context);
}

static int dl_submit(dl_rdp* session, dl_command* command, uint32_t timeout_ms)
{
    if (!session || !command || !timeout_ms || timeout_ms > DL_MAX_TIMEOUT) return 0;
    EnterCriticalSection(&session->lock);
    if (session->fenced || !session->connected || session->count == DL_QUEUE_CAPACITY) {
        LeaveCriticalSection(&session->lock);
        return 0;
    }
    InterlockedIncrement(&command->references);
    session->queue[(session->head + session->count) % DL_QUEUE_CAPACITY] = command;
    session->count++;
    SetEvent(session->queued);
    LeaveCriticalSection(&session->lock);
    if (WaitForSingleObject(command->done, timeout_ms) != WAIT_OBJECT_0) {
        /* A timed-out operation is fenced with the transport, never requeued. */
        dl_fence(session);
        return 0;
    }
    if (!command->result) dl_fence(session);
    return command->result;
}

static dl_command* dl_pop(dl_rdp* session)
{
    dl_command* command = NULL;
    EnterCriticalSection(&session->lock);
    if (session->count && !session->fenced) {
        command = session->queue[session->head];
        session->head = (session->head + 1) % DL_QUEUE_CAPACITY;
        session->count--;
    }
    if (!session->count) ResetEvent(session->queued);
    LeaveCriticalSection(&session->lock);
    return command;
}

static BOOL dl_system_trust(const char* pem, const rdpCertificate* certificate, const char* hostname)
{
#ifdef _WIN32
    HCERTSTORE store = NULL;
    PCCERT_CONTEXT leaf = NULL;
    PCCERT_CHAIN_CONTEXT chain = NULL;
    WCHAR* server = NULL;
    BOOL accepted = FALSE;
    (void)certificate;
    store = CertOpenStore(CERT_STORE_PROV_MEMORY, 0, 0, CERT_STORE_CREATE_NEW_FLAG, NULL);
    if (!store) goto done;
    const char* cursor = pem;
    const char* marker = "-----END CERTIFICATE-----";
    while ((cursor = strstr(cursor, "-----BEGIN CERTIFICATE-----")) != NULL) {
        const char* end = strstr(cursor, marker);
        if (!end) goto done;
        end += strlen(marker);
        DWORD length = (DWORD)(end - cursor);
        DWORD bytes = 0;
        if (!CryptStringToBinaryA(cursor, length, CRYPT_STRING_BASE64HEADER, NULL,
            &bytes, NULL, NULL)) goto done;
        BYTE* der = malloc(bytes);
        if (!der) goto done;
        PCCERT_CONTEXT added = NULL;
        BOOL parsed = CryptStringToBinaryA(cursor, length, CRYPT_STRING_BASE64HEADER,
            der, &bytes, NULL, NULL) && CertAddEncodedCertificateToStore(store,
                X509_ASN_ENCODING | PKCS_7_ASN_ENCODING, der, bytes, CERT_STORE_ADD_ALWAYS, &added);
        free(der);
        if (!parsed) goto done;
        if (!leaf) leaf = added;
        else CertFreeCertificateContext(added);
        cursor = end;
    }
    if (!leaf) goto done;
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, hostname, -1, NULL, 0);
    if (count <= 0) goto done;
    server = calloc((size_t)count, sizeof(WCHAR));
    if (!server || !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        hostname, -1, server, count)) goto done;
    CERT_CHAIN_PARA parameters = { 0 };
    parameters.cbSize = sizeof(parameters);
    LPSTR usage = szOID_PKIX_KP_SERVER_AUTH;
    parameters.RequestedUsage.dwType = USAGE_MATCH_TYPE_AND;
    parameters.RequestedUsage.Usage.cUsageIdentifier = 1;
    parameters.RequestedUsage.Usage.rgpszUsageIdentifier = &usage;
    /* The default system engine supplies trusted roots. Certificates supplied
     * by the peer are additional intermediates, never a trusted-root store. */
    if (!CertGetCertificateChain(NULL, leaf, NULL, store, &parameters, 0, NULL, &chain)) goto done;
    SSL_EXTRA_CERT_CHAIN_POLICY_PARA ssl = { 0 };
    ssl.cbSize = sizeof(ssl);
    ssl.dwAuthType = AUTHTYPE_SERVER;
    ssl.pwszServerName = server;
    CERT_CHAIN_POLICY_PARA policy = { 0 };
    policy.cbSize = sizeof(policy);
    policy.pvExtraPolicyPara = &ssl;
    CERT_CHAIN_POLICY_STATUS status = { 0 };
    status.cbSize = sizeof(status);
    accepted = CertVerifyCertificateChainPolicy(CERT_CHAIN_POLICY_SSL, chain, &policy, &status) &&
        status.dwError == 0 && chain->TrustStatus.dwErrorStatus == CERT_TRUST_NO_ERROR;
done:
    free(server);
    if (chain) CertFreeCertificateChain(chain);
    if (leaf) CertFreeCertificateContext(leaf);
    if (store) CertCloseStore(store, 0);
    return accepted;
#else
    (void)pem;
    return freerdp_certificate_verify(certificate, "/etc/ssl/certs") &&
        freerdp_certificate_matches_hostname(certificate, hostname, strlen(hostname));
#endif
}

static int dl_verify(freerdp* instance, const BYTE* data, size_t length,
    const char* hostname, UINT16 port, DWORD flags)
{
    dl_rdp* session = dl_owner(instance->context);
    rdpCertificate* certificate = NULL;
    char* pem = NULL;
    char* fingerprint = NULL;
    int accepted = 0;
    (void)flags;
    if (!session || !data || !length || length > 1024 * 1024 || !hostname) return 0;
    pem = malloc(length + 1);
    if (!pem) return 0;
    memcpy(pem, data, length);
    pem[length] = 0;
    certificate = freerdp_certificate_new_from_pem(pem);
    if (!certificate) goto done;
    /* FreeRDP checks its authenticated RedirectionAcceptedCert before invoking
     * this callback. A redirect flag alone never grants certificate trust. */
    if (session->accept_certificate) {
        if (!_stricmp(hostname, session->hostname) && port == session->port) accepted = 2;
    } else if (session->pin && *session->pin) {
        if (_stricmp(hostname, session->hostname) || port != session->port) goto done;
        fingerprint = freerdp_certificate_get_fingerprint_by_hash_ex(certificate, "sha256", FALSE);
        if (fingerprint && !_stricmp(fingerprint, session->pin)) accepted = 2;
    } else if (dl_system_trust(pem, certificate, hostname)) {
        accepted = 2;
    }
done:
    if (!accepted) {
        EnterCriticalSection(&session->lock);
        session->certificate_error = !certificate ? 3 : session->accept_certificate ? 4 :
            session->pin && *session->pin ? 2 : 1;
        LeaveCriticalSection(&session->lock);
    }
    free(fingerprint);
    freerdp_certificate_free(certificate);
    free(pem);
    return accepted;
}

static BOOL dl_authenticate(freerdp* instance, char** username, char** password,
    char** domain, rdp_auth_reason reason)
{
    (void)instance; (void)username; (void)password; (void)domain; (void)reason;
    return FALSE; /* Missing/rejected credentials never prompt or retry. */
}

static SSIZE_T dl_retry(freerdp* instance, const char* what, size_t current, void* userarg)
{
    (void)instance; (void)what; (void)current; (void)userarg;
    return -1;
}

static BOOL dl_begin_paint(rdpContext* context)
{
    rdpGdi* gdi = context->gdi;
    if (!gdi || !gdi->primary || !gdi->primary->hdc || !gdi->primary->hdc->hwnd) return FALSE;
    gdi->primary->hdc->hwnd->invalid->null = TRUE;
    return TRUE;
}

static BOOL dl_end_paint(rdpContext* context)
{
    dl_rdp* session = dl_owner(context);
    rdpGdi* gdi = context->gdi;
    if (!session || !gdi || !gdi->primary || !gdi->primary->hdc ||
        !gdi->primary->hdc->hwnd || !gdi->primary_buffer) return FALSE;
    if (dl_stopped(session)) return TRUE;
    HGDI_RGN dirty = gdi->primary->hdc->hwnd->invalid;
    if (!dirty || dirty->null) return TRUE;
    if (gdi->width <= 0 || gdi->height <= 0 || gdi->width > 8192 || gdi->height > 8192 ||
        (uint64_t)gdi->width * (uint64_t)gdi->height > DL_MAX_PIXELS) return FALSE;
    if (dirty->w <= 0 || dirty->h <= 0) return TRUE;
    uint32_t x = dirty->x < 0 ? 0 : (uint32_t)dirty->x;
    uint32_t y = dirty->y < 0 ? 0 : (uint32_t)dirty->y;
    int64_t native_right = (int64_t)dirty->x + dirty->w;
    int64_t native_bottom = (int64_t)dirty->y + dirty->h;
    if (native_right <= 0 || native_bottom <= 0) return TRUE;
    uint32_t right = native_right < gdi->width ? (uint32_t)native_right : (uint32_t)gdi->width;
    uint32_t bottom = native_bottom < gdi->height ? (uint32_t)native_bottom : (uint32_t)gdi->height;
    if (x >= right || y >= bottom) return TRUE;
    EnterCriticalSection(&session->lock);
    const uint64_t generation = session->generation;
    LeaveCriticalSection(&session->lock);
    /* Copying/encoding is owned by the existing Python LiveFrames callback. */
    if (session->frame) session->frame(session->user, gdi->primary_buffer,
        (uint32_t)gdi->width, (uint32_t)gdi->height, gdi->stride, x, y,
        right - x, bottom - y, generation);
    return TRUE;
}

static BOOL dl_resize(rdpContext* context)
{
    dl_rdp* session = dl_owner(context);
    const uint32_t width = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopWidth);
    const uint32_t height = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopHeight);
    if (!width || !height || width > 8192 || height > 8192 ||
        (uint64_t)width * height > DL_MAX_PIXELS || !gdi_resize(context->gdi, width, height)) return FALSE;
    EnterCriticalSection(&session->lock);
    session->generation++;
    LeaveCriticalSection(&session->lock);
    return TRUE;
}

static void dl_clip_pending_complete(dl_rdp* session, BOOL writing, int success)
{
    EnterCriticalSection(&session->lock);
    dl_command** slot = writing ? &session->clipboard_write : &session->clipboard_read;
    dl_command* command = *slot;
    *slot = NULL;
    if (command) { dl_complete(command, success); dl_command_unref(command); }
    LeaveCriticalSection(&session->lock);
}

static void dl_clip_send_pending(dl_rdp* session)
{
    EnterCriticalSection(&session->lock);
    CliprdrClientContext* clipboard = session->clipboard;
    if (!clipboard || !session->clipboard_ready || session->fenced) {
        LeaveCriticalSection(&session->lock);
        return;
    }
    dl_command* command = session->clipboard_write;
    if (command && !command->clipboard_sent) {
        CLIPRDR_FORMAT format = { CF_UNICODETEXT, NULL };
        CLIPRDR_FORMAT_LIST list = { 0 };
        list.common.msgType = CB_FORMAT_LIST;
        list.numFormats = 1;
        list.formats = &format;
        command->clipboard_sent = TRUE;
        if (clipboard->ClientFormatList(clipboard, &list) != CHANNEL_RC_OK)
            dl_clip_pending_complete(session, TRUE, 0);
    }
    command = session->clipboard_read;
    if (command && !command->clipboard_sent) {
        if (session->remote_text) {
            CLIPRDR_FORMAT_DATA_REQUEST request = { 0 };
            request.common.msgType = CB_FORMAT_DATA_REQUEST;
            request.requestedFormatId = CF_UNICODETEXT;
            command->clipboard_sent = TRUE;
            if (clipboard->ClientFormatDataRequest(clipboard, &request) != CHANNEL_RC_OK)
                dl_clip_pending_complete(session, FALSE, 0);
        } else {
            command->available = session->local_owner && session->local_text;
            int result = 1;
            if (command->available) {
                if (session->local_units > command->capacity) result = 0;
                else {
                    command->units = session->local_units;
                    memcpy(command->text, session->local_text, (size_t)command->units * 2);
                }
            }
            dl_clip_pending_complete(session, FALSE, result);
        }
    }
    LeaveCriticalSection(&session->lock);
}

static UINT dl_clip_monitor_ready(CliprdrClientContext* clipboard, const CLIPRDR_MONITOR_READY* ready)
{
    dl_rdp* session = clipboard->custom;
    CLIPRDR_GENERAL_CAPABILITY_SET general = { 0 };
    CLIPRDR_CAPABILITIES capabilities = { 0 };
    (void)ready;
    general.capabilitySetType = CB_CAPSTYPE_GENERAL;
    general.capabilitySetLength = CB_CAPSTYPE_GENERAL_LEN;
    general.version = CB_CAPS_VERSION_2;
    general.generalFlags = CB_USE_LONG_FORMAT_NAMES;
    capabilities.common.msgType = CB_CLIP_CAPS;
    capabilities.cCapabilitiesSets = 1;
    capabilities.capabilitySets = (CLIPRDR_CAPABILITY_SET*)&general;
    UINT result = clipboard->ClientCapabilities(clipboard, &capabilities);
    if (result == CHANNEL_RC_OK) {
        EnterCriticalSection(&session->lock);
        session->clipboard_ready = TRUE;
        LeaveCriticalSection(&session->lock);
        dl_clip_send_pending(session);
    }
    return result;
}

static UINT dl_clip_capabilities(CliprdrClientContext* clipboard, const CLIPRDR_CAPABILITIES* caps)
{
    (void)clipboard; (void)caps;
    return CHANNEL_RC_OK; /* Never enable file transfer capabilities. */
}

static UINT dl_clip_server_list(CliprdrClientContext* clipboard, const CLIPRDR_FORMAT_LIST* list)
{
    dl_rdp* session = clipboard->custom;
    BOOL unicode_text = FALSE;
    CLIPRDR_FORMAT_LIST_RESPONSE response = { 0 };
    for (UINT32 i = 0; i < list->numFormats; i++)
        if (list->formats[i].formatId == CF_UNICODETEXT) unicode_text = TRUE;
    EnterCriticalSection(&session->lock);
    session->remote_text = unicode_text;
    session->local_owner = FALSE;
    LeaveCriticalSection(&session->lock);
    response.common.msgType = CB_FORMAT_LIST_RESPONSE;
    response.common.msgFlags = CB_RESPONSE_OK;
    /* Receiving a format list never reads content without an explicit read. */
    return clipboard->ClientFormatListResponse(clipboard, &response);
}

static UINT dl_clip_list_response(CliprdrClientContext* clipboard,
    const CLIPRDR_FORMAT_LIST_RESPONSE* response)
{
    dl_rdp* session = clipboard->custom;
    BOOL success = (response->common.msgFlags & CB_RESPONSE_OK) != 0;
    EnterCriticalSection(&session->lock);
    if (success && session->clipboard_write) session->local_owner = TRUE;
    LeaveCriticalSection(&session->lock);
    dl_clip_pending_complete(session, TRUE, success ? 1 : 0);
    return CHANNEL_RC_OK;
}

static UINT dl_clip_data_request(CliprdrClientContext* clipboard,
    const CLIPRDR_FORMAT_DATA_REQUEST* request)
{
    dl_rdp* session = clipboard->custom;
    CLIPRDR_FORMAT_DATA_RESPONSE response = { 0 };
    response.common.msgType = CB_FORMAT_DATA_RESPONSE;
    response.common.msgFlags = CB_RESPONSE_FAIL;
    EnterCriticalSection(&session->lock);
    if (!session->fenced && request->requestedFormatId == CF_UNICODETEXT && session->local_text) {
        response.common.msgFlags = CB_RESPONSE_OK;
        response.common.dataLen = (session->local_units + 1) * 2;
        response.requestedFormatData = (const BYTE*)session->local_text;
    }
    UINT result = clipboard->ClientFormatDataResponse(clipboard, &response);
    LeaveCriticalSection(&session->lock);
    return result;
}

static UINT dl_clip_data_response(CliprdrClientContext* clipboard,
    const CLIPRDR_FORMAT_DATA_RESPONSE* response)
{
    dl_rdp* session = clipboard->custom;
    EnterCriticalSection(&session->lock);
    dl_command* command = session->clipboard_read;
    session->clipboard_read = NULL;
    if (command) {
        int result = 0;
        uint32_t bytes = response->common.dataLen;
        if (!(response->common.msgFlags & CB_RESPONSE_OK)) {
            command->available = FALSE;
            result = 1;
        } else if (bytes <= (DL_RDP_MAX_CLIPBOARD_UNITS + 1) * 2 && !(bytes & 1) &&
            (bytes == 0 || response->requestedFormatData)) {
            const BYTE* data = response->requestedFormatData;
            uint32_t count = 0;
            /* CF_UNICODETEXT ends at its first NUL; never expose allocation
             * padding or stale bytes after it as visible clipboard content. */
            while (count < bytes / 2 && (data[count * 2] || data[count * 2 + 1])) count++;
            if (count <= command->capacity) {
                if (count) memcpy(command->text, data, (size_t)count * 2);
            }
            if (count <= command->capacity && dl_utf16(command->text, count, DL_RDP_MAX_CLIPBOARD_UNITS)) {
                command->units = count;
                command->available = TRUE;
                result = 1;
            }
        }
        dl_complete(command, result);
        dl_command_unref(command);
    }
    LeaveCriticalSection(&session->lock);
    return CHANNEL_RC_OK;
}

static void dl_channel_connected(void* context, const ChannelConnectedEventArgs* event)
{
    dl_rdp* session = dl_owner(context);
    if (!strcmp(event->name, CLIPRDR_SVC_CHANNEL_NAME)) {
        CliprdrClientContext* clipboard = event->pInterface;
        clipboard->custom = session;
        clipboard->MonitorReady = dl_clip_monitor_ready;
        clipboard->ServerCapabilities = dl_clip_capabilities;
        clipboard->ServerFormatList = dl_clip_server_list;
        clipboard->ServerFormatListResponse = dl_clip_list_response;
        clipboard->ServerFormatDataRequest = dl_clip_data_request;
        clipboard->ServerFormatDataResponse = dl_clip_data_response;
        EnterCriticalSection(&session->lock);
        session->clipboard = clipboard;
        LeaveCriticalSection(&session->lock);
    } else freerdp_client_OnChannelConnectedEventHandler(context, event);
}

static void dl_channel_disconnected(void* context, const ChannelDisconnectedEventArgs* event)
{
    dl_rdp* session = dl_owner(context);
    if (!strcmp(event->name, CLIPRDR_SVC_CHANNEL_NAME)) {
        EnterCriticalSection(&session->lock);
        session->clipboard = NULL;
        session->clipboard_ready = FALSE;
        session->remote_text = session->local_owner = FALSE;
        LeaveCriticalSection(&session->lock);
        dl_clip_pending_complete(session, TRUE, 0);
        dl_clip_pending_complete(session, FALSE, 0);
    } else freerdp_client_OnChannelDisconnectedEventHandler(context, event);
}

static BOOL dl_pre_connect(freerdp* instance)
{
    return PubSub_SubscribeChannelConnected(instance->context->pubSub, dl_channel_connected) >= 0 &&
        PubSub_SubscribeChannelDisconnected(instance->context->pubSub, dl_channel_disconnected) >= 0;
}

static BOOL dl_post_connect(freerdp* instance)
{
    dl_rdp* session = dl_owner(instance->context);
    if (!gdi_init(instance, PIXEL_FORMAT_BGRX32)) return FALSE;
    if (!freerdp_settings_set_bool(instance->context->settings, FreeRDP_DeactivateClientDecoding, FALSE)) return FALSE;
    EnterCriticalSection(&session->lock);
    session->generation++;
    LeaveCriticalSection(&session->lock);
    instance->context->update->BeginPaint = dl_begin_paint;
    instance->context->update->EndPaint = dl_end_paint;
    instance->context->update->DesktopResize = dl_resize;
    return TRUE;
}

static void dl_post_disconnect(freerdp* instance)
{
    PubSub_UnsubscribeChannelConnected(instance->context->pubSub, dl_channel_connected);
    PubSub_UnsubscribeChannelDisconnected(instance->context->pubSub, dl_channel_disconnected);
    gdi_free(instance);
}

static BOOL dl_client_new(freerdp* instance, rdpContext* context)
{
    (void)context;
    instance->PreConnect = dl_pre_connect;
    instance->PostConnect = dl_post_connect;
    instance->PostDisconnect = dl_post_disconnect;
    instance->VerifyX509Certificate = dl_verify;
    instance->AuthenticateEx = dl_authenticate;
    instance->RetryDialog = dl_retry;
    return TRUE;
}

static BOOL dl_send_key(dl_rdp* session, uint32_t value, BOOL unicode_key, BOOL down)
{
    /* An already-started batch may become unknown while waiting for native IO.
     * UP cleanup remains possible, but no later DOWN from it may be sent. */
    if (down && dl_stopped(session)) return FALSE;
    BYTE* held = unicode_key ? session->held_unicode : session->held_scancodes;
    const BOOL repeat = held[value] != 0;
    if (!down && !held[value]) return TRUE;
    if (down) held[value] = 1; /* A failed write may already have reached the peer. */
    BOOL result = unicode_key ?
        freerdp_input_send_unicode_keyboard_event(session->context->input,
            down ? 0 : KBD_FLAGS_RELEASE, (UINT16)value) :
        freerdp_input_send_keyboard_event_ex(session->context->input, down, repeat, value);
    if (result && !down) held[value] = 0;
    return result;
}

static BOOL dl_release(dl_rdp* session)
{
    BOOL result = TRUE;
    for (uint32_t i = 0; i < 512; i++)
        if (session->held_scancodes[i] && !dl_send_key(session, i, FALSE, FALSE)) result = FALSE;
    for (uint32_t i = 0; i < 65536; i++)
        if (session->held_unicode[i] && !dl_send_key(session, i, TRUE, FALSE)) result = FALSE;
    const uint32_t bits[] = { 1, 2, 4 };
    const UINT16 flags[] = { PTR_FLAGS_BUTTON1, PTR_FLAGS_BUTTON3, PTR_FLAGS_BUTTON2 };
    for (size_t i = 0; i < 3; i++) if (session->buttons & bits[i]) {
        if (freerdp_input_send_mouse_event(session->context->input, flags[i],
            (UINT16)session->x, (UINT16)session->y)) session->buttons &= ~bits[i];
        else result = FALSE;
    }
    return result;
}

static BOOL dl_pointer(dl_rdp* session, dl_command* command)
{
    if (dl_stopped(session)) return FALSE;
    rdpGdi* gdi = session->context->gdi;
    if (!gdi || command->x >= (uint32_t)gdi->width || command->y >= (uint32_t)gdi->height) return FALSE;
    session->x = command->x; session->y = command->y;
    if (!freerdp_input_send_mouse_event(session->context->input, PTR_FLAGS_MOVE,
        (UINT16)command->x, (UINT16)command->y)) return FALSE;
    const uint32_t bits[] = { 1, 2, 4 };
    const UINT16 flags[] = { PTR_FLAGS_BUTTON1, PTR_FLAGS_BUTTON3, PTR_FLAGS_BUTTON2 };
    for (size_t i = 0; i < 3; i++) if ((command->buttons ^ session->buttons) & bits[i]) {
        const BOOL down = (command->buttons & bits[i]) != 0;
        if (down && dl_stopped(session)) return FALSE;
        if (down) session->buttons |= bits[i];
        if (!freerdp_input_send_mouse_event(session->context->input,
            flags[i] | (down ? PTR_FLAGS_DOWN : 0), (UINT16)command->x, (UINT16)command->y)) return FALSE;
        if (!down) session->buttons &= ~bits[i];
    }
    const int count = command->wheel_steps < 0 ? -command->wheel_steps : command->wheel_steps;
    const int rotation = command->wheel_steps > 0 ? -120 : 120;
    for (int i = 0; i < count; i++)
        if (dl_stopped(session) || !freerdp_input_send_mouse_event(session->context->input,
            PTR_FLAGS_WHEEL | ((UINT16)rotation & WheelRotationMask),
            (UINT16)command->x, (UINT16)command->y)) return FALSE;
    return TRUE;
}

static BOOL dl_clipboard_dispatch(dl_rdp* session, dl_command* command)
{
    BOOL deferred = FALSE;
    EnterCriticalSection(&session->lock);
    if (session->fenced) goto done;
    if (command->type == DL_CLIP_WRITE) {
        if (session->clipboard_write) goto done;
        uint16_t* text = calloc((size_t)command->units + 1, sizeof(uint16_t));
        if (!text) goto done;
        if (command->units) memcpy(text, command->text, (size_t)command->units * 2);
        if (session->local_text) {
            SecureZeroMemory(session->local_text, ((size_t)session->local_units + 1) * 2);
            free(session->local_text);
        }
        session->local_text = text;
        session->local_units = command->units;
        session->local_owner = FALSE;
        session->clipboard_write = command;
        InterlockedIncrement(&command->references);
        deferred = TRUE;
    } else {
        if (session->clipboard_read) goto done;
        session->clipboard_read = command;
        InterlockedIncrement(&command->references);
        deferred = TRUE;
    }
done:
    LeaveCriticalSection(&session->lock);
    if (deferred) dl_clip_send_pending(session);
    else dl_complete(command, 0);
    return deferred;
}

static void dl_dispatch(dl_rdp* session, dl_command* command)
{
    if (dl_stopped(session)) { dl_complete(command, 0); return; }
    BOOL success = FALSE;
    switch (command->type) {
        case DL_POINTER: success = dl_pointer(session, command); break;
        case DL_KEY: success = dl_send_key(session, command->value, command->unicode_key, command->down); break;
        case DL_TEXT:
            success = TRUE;
            for (uint32_t i = 0; i < command->units && success; i++) {
                success = dl_send_key(session, command->text[i], TRUE, TRUE);
                if (success) success = dl_send_key(session, command->text[i], TRUE, FALSE);
            }
            break;
        case DL_RELEASE: success = dl_release(session); break;
        case DL_CLIP_WRITE:
        case DL_CLIP_READ:
            (void)dl_clipboard_dispatch(session, command);
            return;
    }
    dl_complete(command, success ? 1 : 0);
    if (!success) dl_fence(session);
}

static DWORD WINAPI dl_run(void* value)
{
    dl_rdp* session = value;
    freerdp* instance = session->context->instance;
    BOOL connected = freerdp_connect(instance);
    EnterCriticalSection(&session->lock);
    session->connected = connected && !session->fenced;
    session->last_error = freerdp_get_last_error(session->context);
    SetEvent(session->ready);
    LeaveCriticalSection(&session->lock);
    while (connected && WaitForSingleObject(session->stop, 0) != WAIT_OBJECT_0 &&
        !freerdp_shall_disconnect_context(session->context)) {
        HANDLE handles[MAXIMUM_WAIT_OBJECTS] = { 0 };
        DWORD count = freerdp_get_event_handles(session->context, handles, MAXIMUM_WAIT_OBJECTS - 2);
        if (!count) break;
        handles[count++] = session->stop;
        handles[count++] = session->queued;
        DWORD result = WaitForMultipleObjects(count, handles, FALSE, INFINITE);
        if (result == WAIT_FAILED || WaitForSingleObject(session->stop, 0) == WAIT_OBJECT_0) break;
        if (!freerdp_check_event_handles(session->context)) break;
        dl_command* command;
        while ((command = dl_pop(session)) != NULL) {
            dl_dispatch(session, command);
            dl_command_unref(command);
        }
    }
    /* Best effort UP before disconnect when the transport is still usable. */
    EnterCriticalSection(&session->lock);
    if (!session->last_error) session->last_error = freerdp_get_last_error(session->context);
    LeaveCriticalSection(&session->lock);
    if (connected && !freerdp_shall_disconnect_context(session->context)) (void)dl_release(session);
    dl_fence(session);
    freerdp_disconnect(instance);
    EnterCriticalSection(&session->lock);
    session->connected = FALSE;
    LeaveCriticalSection(&session->lock);
    if (session->closed) session->closed(session->user);
    return 0;
}

uint32_t dl_rdp_abi_version(void) { return DL_RDP_ABI_VERSION; }

uint32_t dl_rdp_keycode(const char* name)
{
    if (!name || strlen(name) > 48) return 0;
    char normalized[16];
    if (strlen(name) == 4 && !strncmp(name, "VK_", 3) &&
        ((name[3] >= 'A' && name[3] <= 'Z') || (name[3] >= '0' && name[3] <= '9'))) {
        (void)snprintf(normalized, sizeof(normalized), "VK_KEY_%c", name[3]);
        name = normalized;
    }
    DWORD vk = GetVirtualKeyCodeFromName(name);
    switch (vk) {
        case VK_LEFT: case VK_RIGHT: case VK_UP: case VK_DOWN:
        case VK_DELETE: case VK_INSERT: case VK_HOME: case VK_END:
        case VK_PRIOR: case VK_NEXT: case VK_RCONTROL: case VK_RMENU:
            return GetVirtualScanCodeFromVirtualKeyCode(vk | KBDEXT,
                WINPR_KBD_TYPE_IBM_ENHANCED);
    }
    DWORD scancode = GetVirtualScanCodeFromVirtualKeyCode(vk, WINPR_KBD_TYPE_IBM_ENHANCED);
    /* WinPR's documented reverse map selects normal/extended tables by the
     * VK's KBDEXT flag. Preserve an existing ordinary Enter/Tab mapping. */
    if (!scancode) scancode = GetVirtualScanCodeFromVirtualKeyCode(vk | KBDEXT,
        WINPR_KBD_TYPE_IBM_ENHANCED);
    return scancode;
}

dl_rdp* dl_rdp_new(const dl_rdp_options* options, dl_rdp_frame_cb frame,
    dl_rdp_closed_cb closed, void* user)
{
    if (!options || options->size != sizeof(*options) || options->abi_version != DL_RDP_ABI_VERSION ||
        !options->hostname || !*options->hostname || strlen(options->hostname) > 1024 ||
        !options->port || options->port > 65535 || !options->width || !options->height ||
        options->width > 8192 || options->height > 8192 ||
        (uint64_t)options->width * options->height > DL_MAX_PIXELS) return NULL;
    BOOL accept_certificate = options->certificate_sha256 &&
        !strcmp(options->certificate_sha256, "accept-current");
    if (!accept_certificate && options->certificate_sha256 && *options->certificate_sha256) {
        if (strlen(options->certificate_sha256) != 64) return NULL;
        for (const char* p = options->certificate_sha256; *p; p++)
            if (!isxdigit((unsigned char)*p)) return NULL;
    }
    dl_rdp* session = calloc(1, sizeof(*session));
    if (!session) return NULL;
    InitializeCriticalSection(&session->lock);
    session->stop = CreateEvent(NULL, TRUE, FALSE, NULL);
    session->ready = CreateEvent(NULL, TRUE, FALSE, NULL);
    session->queued = CreateEvent(NULL, TRUE, FALSE, NULL);
    session->hostname = _strdup(options->hostname);
    session->accept_certificate = accept_certificate;
    session->pin = _strdup(!accept_certificate && options->certificate_sha256 ? options->certificate_sha256 : "");
    session->port = options->port;
    session->frame = frame; session->closed = closed; session->user = user;
    if (!session->stop || !session->ready || !session->queued || !session->hostname || !session->pin) goto fail;
    /* Error-level native diagnostics go only to the worker's private stderr.
     * The parent retains/redacts startup output and discards it after connect. */
    wLog* log = WLog_GetRoot();
    if (!log || !WLog_SetLogAppenderType(log, WLOG_APPENDER_CONSOLE) ||
        !WLog_ConfigureAppender(WLog_GetLogAppender(log), "outputstream", (void*)"stderr") ||
        !WLog_SetLogLevel(log, WLOG_ERROR)) goto fail;
    RDP_CLIENT_ENTRY_POINTS entry = { 0 };
    entry.Size = sizeof(entry); entry.Version = RDP_CLIENT_INTERFACE_VERSION;
    entry.ContextSize = sizeof(dl_context); entry.ClientNew = dl_client_new;
    session->context = freerdp_client_context_new(&entry);
    if (!session->context) goto fail;
    ((dl_context*)session->context)->owner = session;
    rdpSettings* settings = session->context->settings;
    const FreeRDP_Settings_Keys_Bool disabled[] = {
        FreeRDP_DeviceRedirection, FreeRDP_RedirectDrives, FreeRDP_RedirectHomeDrive,
        FreeRDP_RedirectSerialPorts, FreeRDP_RedirectParallelPorts, FreeRDP_RedirectSmartCards,
        FreeRDP_RedirectPrinters, FreeRDP_AudioPlayback, FreeRDP_AudioCapture,
        FreeRDP_RemoteConsoleAudio, FreeRDP_NetworkAutoDetect, FreeRDP_SupportHeartbeatPdu,
        FreeRDP_SupportMultitransport, FreeRDP_UseCommonStdioCallbacks,
        FreeRDP_AutoReconnectionEnabled, FreeRDP_IgnoreCertificate, FreeRDP_AutoAcceptCertificate,
        FreeRDP_RdpSecurity
    };
    for (size_t i = 0; i < sizeof(disabled) / sizeof(disabled[0]); i++)
        if (!freerdp_settings_set_bool(settings, disabled[i], FALSE)) goto fail;
    const FreeRDP_Settings_Keys_Bool enabled[] = {
        FreeRDP_ExternalCertificateManagement, FreeRDP_CertificateCallbackPreferPEM,
        FreeRDP_SupportGraphicsPipeline, FreeRDP_SupportDisplayControl,
        FreeRDP_RedirectClipboard, FreeRDP_GfxH264, FreeRDP_GfxAVC444
    };
    for (size_t i = 0; i < sizeof(enabled) / sizeof(enabled[0]); i++)
        if (!freerdp_settings_set_bool(settings, enabled[i], TRUE)) goto fail;
    if (!freerdp_settings_set_string(settings, FreeRDP_ServerHostname, options->hostname) ||
        !freerdp_settings_set_string(settings, FreeRDP_Username, options->username ? options->username : "") ||
        !freerdp_settings_set_string(settings, FreeRDP_Password, options->password ? options->password : "") ||
        !freerdp_settings_set_string(settings, FreeRDP_Domain, options->domain ? options->domain : "") ||
        !freerdp_settings_set_uint32(settings, FreeRDP_ServerPort, options->port) ||
        !freerdp_settings_set_uint32(settings, FreeRDP_DesktopWidth, options->width) ||
        !freerdp_settings_set_uint32(settings, FreeRDP_DesktopHeight, options->height) ||
        !freerdp_settings_set_uint32(settings, FreeRDP_ColorDepth, 32) ||
        !freerdp_settings_set_uint32(settings, FreeRDP_AuthenticationLevel, 2) ||
        !freerdp_settings_set_uint32(settings, FreeRDP_TcpConnectTimeout, 10000)) goto fail;
    return session;
fail:
    dl_rdp_free(session);
    return NULL;
}

int dl_rdp_start(dl_rdp* session, uint32_t timeout_ms)
{
    if (!session || session->thread || !timeout_ms || timeout_ms > DL_MAX_TIMEOUT) return 0;
    session->thread = CreateThread(NULL, 0, dl_run, session, 0, NULL);
    if (!session->thread) return 0;
    if (WaitForSingleObject(session->ready, timeout_ms) != WAIT_OBJECT_0) { dl_fence(session); return 0; }
    EnterCriticalSection(&session->lock);
    int connected = session->connected && !session->fenced;
    LeaveCriticalSection(&session->lock);
    return connected;
}

uint32_t dl_rdp_error(dl_rdp* session, char* reason, uint32_t capacity)
{
    if (!reason || !capacity) return 0;
    reason[0] = 0;
    if (!session) return 0;
    EnterCriticalSection(&session->lock);
    uint32_t code = session->last_error, certificate = session->certificate_error;
    LeaveCriticalSection(&session->lock);
    const char* name = freerdp_get_last_error_name(code);
    const char* description = freerdp_get_last_error_string(code);
    (void)snprintf(reason, capacity, "%s%s: %s",
        certificate == 4 ? "certificate: endpoint mismatch; " :
        certificate == 3 ? "certificate: server certificate could not be parsed; " :
        certificate == 2 ? "certificate: pinned SHA-256 or endpoint mismatch; " :
        certificate == 1 ? "certificate: system trust or hostname verification rejected; " : "",
        name ? name : "UNKNOWN_FREERDP_ERROR", description ? description : "No native error description");
    return code;
}

int dl_rdp_pointer(dl_rdp* session, uint32_t x, uint32_t y, uint32_t buttons,
    int32_t wheel_steps, uint32_t timeout_ms)
{
    if (x > 65535 || y > 65535 || buttons > 7 || wheel_steps < -20 || wheel_steps > 20) return 0;
    dl_command* command = dl_command_new(DL_POINTER);
    if (!command) return 0;
    command->x = x; command->y = y; command->buttons = buttons; command->wheel_steps = wheel_steps;
    int result = dl_submit(session, command, timeout_ms); dl_command_unref(command); return result;
}

int dl_rdp_key(dl_rdp* session, uint32_t value, uint32_t unicode_key, uint32_t down, uint32_t timeout_ms)
{
    if (unicode_key > 1 || down > 1 || !value || value > (unicode_key ? 65535u : 511u) ||
        (unicode_key && value >= 0xd800 && value <= 0xdfff)) return 0;
    dl_command* command = dl_command_new(DL_KEY);
    if (!command) return 0;
    command->value = value; command->unicode_key = unicode_key; command->down = down;
    int result = dl_submit(session, command, timeout_ms); dl_command_unref(command); return result;
}

static int dl_submit_text(dl_rdp* session, dl_command_type type, const uint16_t* text,
    uint32_t units, uint32_t limit, uint32_t timeout_ms)
{
    if (!dl_utf16(text, units, limit)) return 0;
    dl_command* command = dl_command_new(type);
    if (!command) return 0;
    if (!dl_command_text(command, text, units, units)) { dl_command_unref(command); return 0; }
    int result = dl_submit(session, command, timeout_ms); dl_command_unref(command); return result;
}

int dl_rdp_text(dl_rdp* session, const uint16_t* text, uint32_t units, uint32_t timeout_ms)
{
    return dl_submit_text(session, DL_TEXT, text, units, DL_RDP_MAX_TEXT_UNITS, timeout_ms);
}

int dl_rdp_clipboard_write(dl_rdp* session, const uint16_t* text, uint32_t units, uint32_t timeout_ms)
{
    return dl_submit_text(session, DL_CLIP_WRITE, text, units, DL_RDP_MAX_CLIPBOARD_UNITS, timeout_ms);
}

int dl_rdp_clipboard_read(dl_rdp* session, uint16_t* text, uint32_t capacity,
    uint32_t* written, uint32_t* available, uint32_t timeout_ms)
{
    if (!written || !available || capacity > DL_RDP_MAX_CLIPBOARD_UNITS || (capacity && !text)) return 0;
    *written = *available = 0;
    dl_command* command = dl_command_new(DL_CLIP_READ);
    if (!command) return 0;
    if (!dl_command_text(command, NULL, 0, capacity)) { dl_command_unref(command); return 0; }
    int result = dl_submit(session, command, timeout_ms);
    if (result) {
        *written = command->units; *available = command->available;
        if (command->units) memcpy(text, command->text, (size_t)command->units * 2);
    }
    dl_command_unref(command);
    return result;
}

int dl_rdp_release_all(dl_rdp* session, uint32_t timeout_ms)
{
    dl_command* command = dl_command_new(DL_RELEASE);
    if (!command) return 0;
    int result = dl_submit(session, command, timeout_ms); dl_command_unref(command); return result;
}

void dl_rdp_free(dl_rdp* session)
{
    if (!session) return;
    dl_fence(session);
    /* The existing isolated worker is the outer hard timeout boundary. Never
     * free a context/callback while native IO can still reference it. */
    if (session->thread) { WaitForSingleObject(session->thread, INFINITE); CloseHandle(session->thread); }
    if (session->context) freerdp_client_context_free(session->context);
    if (session->local_text) {
        SecureZeroMemory(session->local_text, ((size_t)session->local_units + 1) * 2);
        free(session->local_text);
    }
    free(session->hostname); free(session->pin);
    if (session->stop) CloseHandle(session->stop);
    if (session->ready) CloseHandle(session->ready);
    if (session->queued) CloseHandle(session->queued);
    DeleteCriticalSection(&session->lock);
    free(session);
}
