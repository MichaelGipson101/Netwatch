# 2026-06-29 — NAS Machine V3 Intermittent Flapping

## Summary

NAS Machine V3 (192.168.6.60) dropped three times between 14:14 and 14:35, taking its hosted
VMs (TrueNAS at 192.168.6.125, EmailArchive at 192.168.6.118) down with it each time. Root
cause: EEE (Energy Efficient Ethernet) on the host NIC causing brief link interruptions.

## Outage Timeline

| Time (local) | Event |
|---|---|
| 14:15:05 | NAS Machine V3 begins dropping pings |
| 14:15:38 | Brief recovery at 1016ms latency (NIC struggling) |
| 14:16:29 | Netwatch declares host down, ntfy alert sent |
| 14:17:01 | Recovered |
| 14:27:24 | Second drop (~32s) |
| 14:27:56 | Recovered |
| 14:35:26 | Third drop (~15s) |
| 14:35:41 | Recovered — no further flaps as of 15:30 |

## Cascade Effects

- **TrueNAS** (VMID 121, LXC on NASMachineV3) dropped with each NAS flap
- **EmailArchive** (VMID 123, LXC on NASMachineV3) dropped with each NAS flap
- **ProxmoxPoller** timed out at 14:16:15, 14:27:30, 14:34:51 (coincident with each flap)
- Proxmox cluster (corosync) lost heartbeat with NASMachineV3 during flaps, triggering
  "Netwatch · Proxmox Alert" ntfy notifications at 14:31:40 and 15:22:07

## Investigation Notes

- All other hosts that appeared to drop during the same windows were simply offline (unused
  machines not in use, not related to the outage)
- The Pi5's own NIC (eth0 / macb driver) was ruled out: no kernel link-down events in dmesg
  or journalctl during the outage windows; ethtool stats clean
- Previous EEE-caused downtime on NASMachineV3 was already diagnosed before this incident

## Fix (Already Applied)

`/etc/network/interfaces` on NASMachineV3 contains:

```
post-up ethtool --set-eee enp3s0 eee off
```

This disables EEE on the physical NIC (`enp3s0`) at every boot. Confirmed via Proxmox API
network config query — the option is present and active.

## Secondary Findings

**EmailArchive vzstart warning (13:20:15):**
After a manual stopall/startall cycle on NASMachineV3 at 13:13, EmailArchive's container
start logged: `WARN: Systemd 252 detected. You may need to enable nesting.`
Container is running fine. If systemd-related service failures occur inside EmailArchive,
enable nesting: `pct set 123 --features nesting=1`

**Proxmox apt-get failures on `pve` node:**
Two `apt-get update` failures logged (exit code 100) at 04:17 and 04:59. Unrelated to this
incident but worth checking repo connectivity on the pve node.
