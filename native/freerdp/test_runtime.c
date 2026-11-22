/* SPDX-License-Identifier: Apache-2.0
 * Offline runtime safety checks. All FreeRDP input/abort entry points are stubs.
 */
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdio.h>
#include <freerdp/input.h>
#include <freerdp/freerdp.h>
static unsigned sends, fail_at, stop_after;
static HANDLE stop_on_send;
static struct { unsigned kind, value, down, flags; } emitted[64];
static BOOL stub_key(rdpInput* input, BOOL down, BOOL repeat, UINT32 code) {
    (void)input; (void)repeat;
    emitted[sends].kind=1; emitted[sends].value=code; emitted[sends].down=down;
    return ++sends != fail_at;
}
static BOOL stub_unicode(rdpInput* input, UINT16 flags, UINT16 code) {
    (void)input;
    emitted[sends].kind=2; emitted[sends].value=code; emitted[sends].flags=flags;
    sends++; if (stop_on_send && sends==stop_after) SetEvent(stop_on_send);
    return sends != fail_at;
}
static BOOL stub_mouse(rdpInput* input, UINT16 flags, UINT16 x, UINT16 y) {
    (void)input; (void)x; (void)y;
    emitted[sends].kind=3; emitted[sends].flags=flags;
    return ++sends != fail_at;
}
static BOOL stub_abort(rdpContext* context) { (void)context; return TRUE; }
#define freerdp_abort_connect_context stub_abort
#define freerdp_input_send_keyboard_event_ex stub_key
#define freerdp_input_send_unicode_keyboard_event stub_unicode
#define freerdp_input_send_mouse_event stub_mouse
#include "decklab_rdp.c"
static void reset_sends(unsigned failure) { sends=0; fail_at=failure; memset(emitted,0,sizeof(emitted)); }

static void check_security_modes(void)
{
    rdpSettings* defaults=freerdp_settings_new(0);assert(defaults);
    assert(freerdp_settings_get_bool(defaults,FreeRDP_TlsSecurity));
    assert(freerdp_settings_get_bool(defaults,FreeRDP_NlaSecurity));
    assert(freerdp_settings_get_bool(defaults,FreeRDP_ExtSecurity));
    assert(freerdp_settings_get_bool(defaults,FreeRDP_RdpSecurity));
    assert(!freerdp_settings_get_bool(defaults,FreeRDP_RdstlsSecurity));
    freerdp_settings_free(defaults);
    fprintf(stderr,"SDK defaults verified: TLS/NLA/Ext/RDP=1, RDSTLS=0\n");
    const char* pins[]={NULL,"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","accept-current"};
    for(unsigned i=0;i<sizeof(pins)/sizeof(pins[0]);i++) {
        dl_rdp_options options={0};options.size=sizeof(options);options.abi_version=DL_RDP_ABI_VERSION;
        options.hostname="offline.decklab.invalid";options.port=3389;options.width=4;options.height=4;
        options.certificate_sha256=pins[i];
        dl_rdp* configured=dl_rdp_new(&options,NULL,NULL,NULL);assert(configured);
        rdpSettings* settings=configured->context->settings;
        assert(freerdp_settings_get_bool(settings,FreeRDP_TlsSecurity));
        assert(freerdp_settings_get_bool(settings,FreeRDP_NlaSecurity));
        assert(freerdp_settings_get_bool(settings,FreeRDP_ExtSecurity));
        assert(!freerdp_settings_get_bool(settings,FreeRDP_RdstlsSecurity));
        assert(!freerdp_settings_get_bool(settings,FreeRDP_RdpSecurity));
        /* The original library may enable RDSTLS for authenticated handover;
         * doing so must not enable standard RDP security or disable TLS/NLA. */
        assert(freerdp_settings_set_bool(settings,FreeRDP_RdstlsSecurity,TRUE));
        assert(freerdp_settings_get_bool(settings,FreeRDP_RdstlsSecurity));
        assert(freerdp_settings_get_bool(settings,FreeRDP_TlsSecurity));
        assert(freerdp_settings_get_bool(settings,FreeRDP_NlaSecurity));
        assert(freerdp_settings_get_bool(settings,FreeRDP_ExtSecurity));
        assert(!freerdp_settings_get_bool(settings,FreeRDP_RdpSecurity));
        assert(configured->accept_certificate==(i==2));
        dl_rdp_free(configured);
    }
}

static void check_certificate_policy(const char* path)
{
    FILE* source=fopen(path,"rb");assert(source);
    BYTE pem[8192];
    size_t length=fread(pem,1,sizeof(pem),source);
    assert(length>0 && length<sizeof(pem) && feof(source));fclose(source);
    dl_rdp_options options={0};options.size=sizeof(options);options.abi_version=DL_RDP_ABI_VERSION;
    options.hostname="offline.decklab.invalid";options.port=3389;options.width=4;options.height=4;
    options.certificate_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    dl_rdp* pinned=dl_rdp_new(&options,NULL,NULL,NULL);assert(pinned);
    assert(dl_verify(pinned->context->instance,pem,length,options.hostname,3389,0)==0);
    dl_rdp_free(pinned);
    options.certificate_sha256="accept-current";
    dl_rdp* accepted=dl_rdp_new(&options,NULL,NULL,NULL);assert(accepted);
    assert(dl_verify(accepted->context->instance,pem,length,options.hostname,3389,0)==2);
    assert(dl_verify(accepted->context->instance,pem,length,options.hostname,3390,0)==0);
    assert(dl_verify(accepted->context->instance,pem,length,"other.decklab.invalid",3389,0)==0);
    dl_rdp_free(accepted);
}

int main(int argc,char** argv) {
    check_security_modes();
    if (argc==2) check_certificate_policy(argv[1]);
    dl_rdp session={0}; rdpContext context={0}; rdpGdi gdi={0};
    InitializeCriticalSection(&session.lock); session.context=&context; context.gdi=&gdi;
    char reason[512];
    session.last_error=FREERDP_ERROR_CONNECT_WRONG_PASSWORD;
    assert(dl_rdp_error(&session,reason,sizeof(reason))==FREERDP_ERROR_CONNECT_WRONG_PASSWORD);
    assert(strstr(reason,"ERRCONNECT_WRONG_PASSWORD"));
    session.certificate_error=2;
    (void)dl_rdp_error(&session,reason,sizeof(reason));assert(strstr(reason,"certificate: pinned SHA-256"));
    char tiny[4];(void)dl_rdp_error(&session,tiny,sizeof(tiny));assert(tiny[3]==0);
    assert(dl_rdp_error(NULL,reason,sizeof(reason))==0 && !reason[0]);
    session.last_error=session.certificate_error=0;
    gdi.width=1280; gdi.height=800;
    const uint16_t pair[]={0xd83d,0xde00};
    const uint16_t high[]={0xd83d}; const uint16_t low[]={0xde00}; const uint16_t nul[]={0};
    assert(dl_utf16(pair,2,2048)); assert(!dl_utf16(high,1,2048));
    assert(!dl_utf16(low,1,2048)); assert(!dl_utf16(nul,1,2048));
    assert(!dl_rdp_key(NULL,0xd83d,1,1,100));
    reset_sends(1);
    assert(!dl_send_key(&session,0x14b,FALSE,TRUE)); assert(session.held_scancodes[0x14b]);
    fail_at=0; assert(dl_release(&session)); assert(!session.held_scancodes[0x14b]);
    assert(sends==2 && emitted[0].down==1 && emitted[1].down==0);
    reset_sends(2); dl_command pointer={0}; pointer.x=20; pointer.y=30; pointer.buttons=1;
    assert(!dl_pointer(&session,&pointer)); assert(session.buttons==1);
    fail_at=0; assert(dl_release(&session)); assert(session.buttons==0);
    assert(sends==3 && (emitted[1].flags&PTR_FLAGS_DOWN) && !(emitted[2].flags&PTR_FLAGS_DOWN));
    reset_sends(0); pointer.buttons=0; pointer.wheel_steps=1;
    assert(dl_pointer(&session,&pointer));
    assert(sends==2 && emitted[1].flags==(PTR_FLAGS_WHEEL|((UINT16)-120&WheelRotationMask)));
    dl_command* command=dl_command_new(DL_TEXT); assert(command);
    assert(dl_command_text(command,pair,2,2)); reset_sends(0); dl_dispatch(&session,command);
    assert(command->result==1 && sends==4);
    assert(emitted[0].value==0xd83d && emitted[1].value==0xd83d &&
           emitted[2].value==0xde00 && emitted[3].value==0xde00);
    assert(emitted[0].flags==0 && emitted[1].flags==KBD_FLAGS_RELEASE);
    dl_command_unref(command);
    /* The real GDM field accepted the Latin characters but omitted the BMP
     * Chinese characters. Check that the adapter does not skip those units. */
    const uint16_t mixed[]={ 'D','e','b','i','a','n',0x4e2d,0x6587,0x9a8c,0x8bc1,'1','1','2','2' };
    const unsigned mixed_units=sizeof(mixed)/sizeof(mixed[0]);
    command=dl_command_new(DL_TEXT);assert(command);
    assert(dl_command_text(command,mixed,mixed_units,mixed_units));
    reset_sends(0);dl_dispatch(&session,command);
    assert(command->completed && command->result && sends==mixed_units*2);
    for(unsigned i=0;i<mixed_units;i++) {
        assert(emitted[i*2].kind==2 && emitted[i*2].value==mixed[i] && emitted[i*2].flags==0);
        assert(emitted[i*2+1].kind==2 && emitted[i*2+1].value==mixed[i] &&
               emitted[i*2+1].flags==KBD_FLAGS_RELEASE && !session.held_unicode[mixed[i]]);
    }
    dl_command_unref(command);
    session.stop=CreateEvent(NULL,TRUE,FALSE,NULL); assert(session.stop);
    stop_on_send=session.stop;stop_after=1;reset_sends(0);
    command=dl_command_new(DL_TEXT);assert(command);assert(dl_command_text(command,pair,2,2));
    dl_dispatch(&session,command);
    assert(command->completed && !command->result && sends==2);
    assert(emitted[0].value==0xd83d && emitted[1].value==0xd83d);
    assert(!session.held_unicode[0xd83d] && !session.held_unicode[0xde00]);
    dl_command_unref(command);stop_on_send=NULL;stop_after=0;
    session.fenced=FALSE;ResetEvent(session.stop);session.connected=TRUE;
    dl_command* full[DL_QUEUE_CAPACITY];
    for(size_t i=0;i<DL_QUEUE_CAPACITY;i++) {
        full[i]=dl_command_new(DL_KEY);assert(full[i]);
        InterlockedIncrement(&full[i]->references);session.queue[i]=full[i];
    }
    session.count=DL_QUEUE_CAPACITY;command=dl_command_new(DL_KEY);assert(command);
    reset_sends(0);assert(!dl_submit(&session,command,10));assert(sends==0);
    dl_command_unref(command);dl_fence(&session);assert(session.count==0);
    for(size_t i=0;i<DL_QUEUE_CAPACITY;i++) {
        assert(full[i]->completed && !full[i]->result);dl_command_unref(full[i]);
    }
    session.fenced=FALSE;ResetEvent(session.stop);
    CliprdrClientContext clip={0}; clip.custom=&session;
    uint16_t wire[]={0x4e2d,0x6587,0,0,0xdead};
    CLIPRDR_FORMAT_DATA_RESPONSE response={0}; response.common.msgFlags=CB_RESPONSE_OK;
    response.common.dataLen=sizeof(wire); response.requestedFormatData=(BYTE*)wire;
    command=dl_command_new(DL_CLIP_READ); assert(command); assert(dl_command_text(command,NULL,0,10));
    InterlockedIncrement(&command->references); session.clipboard_read=command;
    assert(dl_clip_data_response(&clip,&response)==CHANNEL_RC_OK);
    assert(command->result==1 && command->available && command->units==2);
    assert(command->text[0]==0x4e2d && command->text[1]==0x6587);
    dl_command_unref(command);
    wire[0]=0xd83d; wire[1]=0;
    command=dl_command_new(DL_CLIP_READ); assert(command); assert(dl_command_text(command,NULL,0,10));
    InterlockedIncrement(&command->references); session.clipboard_read=command;
    dl_clip_data_response(&clip,&response); assert(command->completed && !command->result);
    dl_command_unref(command);CloseHandle(session.stop); DeleteCriticalSection(&session.lock);
    puts("PASS: UTF16, failed DOWN retention/UP cleanup, wheel direction, clipboard NUL boundary; no network/input");
    return 0;
}
