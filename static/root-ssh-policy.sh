(
if [ "$(id -u)" = 0 ]; then set -- sh; else set -- sudo -n sh; fi
"$@" -s <<'DECKLAB_ROOT_SSH'
set -eu
cfg=/etc/ssh/sshd_config
sshd=$(command -v sshd || printf /usr/sbin/sshd)
reload_sshd() { systemctl reload ssh || systemctl reload sshd; }
effective() {
    settings=$("$sshd" -T -f "$1" -C user=root,host=localhost,addr=127.0.0.1) &&
    printf '%s\n' "$settings" | grep -qx 'permitrootlogin yes' &&
    printf '%s\n' "$settings" | grep -qx 'passwordauthentication yes'
}
tmp=$(mktemp /etc/ssh/.decklab-config.XXXXXX)
backup=
trap 'rm -f "$tmp"; if [ -n "$backup" ]; then rm -f "$backup"; fi' EXIT
if ! effective "$cfg"; then
    backup=$(mktemp /etc/ssh/.decklab-backup.XXXXXX)
    cp -p "$cfg" "$backup"
    cp -p "$cfg" "$tmp"
    { printf '%s\n' 'PermitRootLogin yes' 'PasswordAuthentication yes'; cat "$cfg"; } > "$tmp"
    "$sshd" -t -f "$tmp"
    effective "$tmp" || { echo 'sshd policy still prevents root password login' >&2; exit 1; }
    mv "$tmp" "$cfg"
fi
if ! reload_sshd; then
    if [ -n "$backup" ]; then mv "$backup" "$cfg"; reload_sshd || :; fi
    exit 1
fi
echo SSH_CONFIG_OK=1
DECKLAB_ROOT_SSH
)
