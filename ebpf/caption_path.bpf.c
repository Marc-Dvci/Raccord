// Delivery-path telemetry for the caption chain.
//
// Why this exists: the benchmark's hardest failures are infrastructure causes
// whose media symptoms are identical (docs/BENCHMARK.md section 3). A stale
// configuration, packet loss, provider degradation and an encoder CPU
// saturation all present as caption drift with omissions, and the eight
// most-misdiagnosed faults in the corpus are mostly this class. Application
// metrics cannot separate them because by the time the symptom is visible in a
// caption cue, the distinguishing evidence - which socket blocked, which
// process was descheduled, which clock stepped - has already been lost.
//
// These programs capture that evidence at the kernel boundary, keyed by the
// cgroup of the delivery component, and expose it as histograms the loader
// scrapes into the same Prometheus the probe fleet writes to. The change
// correlation agent then has infrastructure evidence on the same timeline as
// the media symptom.
//
// Four attachment points, chosen because each maps to a documented fault class:
//
//   sched:sched_switch      encoder descheduling  -> infra.encoder_cpu
//   tcp_retransmit_skb      caption path loss     -> infra.packet_loss
//   sys_enter_clock_adjtime timing reference step -> infra.clock_source_change
//   sys_enter_openat        config file reload    -> infra.stale_config
//
// Nothing here reads payload. The programs record timings, counts and cgroup
// ids - never packet contents, never a viewer, never a session. See
// docs/PRIVACY.md.
//
// Build:  clang -O2 -g -target bpf -c caption_path.bpf.c -o caption_path.bpf.o
// Load:   python ebpf/loader.py --cgroup /sys/fs/cgroup/media/caption-encoder

#include <vmlinux.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

char LICENSE[] SEC("license") = "Apache-2.0";

#define MAX_COMPONENTS 64
#define SLOTS 27  // log2 histogram, up to ~1 minute in nanoseconds

// A component is identified by its cgroup id, which the loader maps back to a
// delivery-chain component name. Nothing more granular is recorded.
struct hist {
  __u64 slots[SLOTS];
};

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, MAX_COMPONENTS);
  __type(key, __u64);    // cgroup id
  __type(value, struct hist);
} offcpu_ns SEC(".maps");

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, MAX_COMPONENTS);
  __type(key, __u64);
  __type(value, __u64);
} retransmits SEC(".maps");

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, MAX_COMPONENTS);
  __type(key, __u64);
  __type(value, __u64);
} clock_adjustments SEC(".maps");

struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, MAX_COMPONENTS);
  __type(key, __u64);
  __type(value, __u64);
} config_reopens SEC(".maps");

// pid -> nanoseconds at which the task left the CPU
struct {
  __uint(type, BPF_MAP_TYPE_HASH);
  __uint(max_entries, 16384);
  __type(key, __u32);
  __type(value, __u64);
} off_at SEC(".maps");

static __always_inline __u32 log2_slot(__u64 value) {
  __u32 slot = 0;
#pragma unroll
  for (int i = 0; i < SLOTS - 1; i++) {
    if (value <= 1) break;
    value >>= 1;
    slot++;
  }
  return slot;
}

static __always_inline void bump(void *map, __u64 key) {
  __u64 zero = 0;
  __u64 *count = bpf_map_lookup_elem(map, &key);
  if (!count) {
    bpf_map_update_elem(map, &key, &zero, BPF_NOEXIST);
    count = bpf_map_lookup_elem(map, &key);
    if (!count) return;
  }
  __sync_fetch_and_add(count, 1);
}

// --- encoder descheduling -------------------------------------------------
//
// A caption encoder that is off-CPU for tens of milliseconds at a time emits
// cues late. That is indistinguishable, from the media side, from an upstream
// timing problem - which is precisely the confusion in the benchmark's hardest
// band.
SEC("tp_btf/sched_switch")
int BPF_PROG(on_sched_switch, bool preempt, struct task_struct *prev,
             struct task_struct *next) {
  __u64 now = bpf_ktime_get_ns();
  __u32 prev_pid = BPF_CORE_READ(prev, pid);
  __u32 next_pid = BPF_CORE_READ(next, pid);

  bpf_map_update_elem(&off_at, &prev_pid, &now, BPF_ANY);

  __u64 *since = bpf_map_lookup_elem(&off_at, &next_pid);
  if (!since) return 0;
  __u64 delta = now - *since;
  bpf_map_delete_elem(&off_at, &next_pid);
  if (delta < 1000000) return 0;  // ignore sub-millisecond scheduling noise

  __u64 cgroup = bpf_get_current_cgroup_id();
  struct hist *h = bpf_map_lookup_elem(&offcpu_ns, &cgroup);
  if (!h) {
    struct hist empty = {};
    bpf_map_update_elem(&offcpu_ns, &cgroup, &empty, BPF_NOEXIST);
    h = bpf_map_lookup_elem(&offcpu_ns, &cgroup);
    if (!h) return 0;
  }
  __u32 slot = log2_slot(delta);
  if (slot < SLOTS) __sync_fetch_and_add(&h->slots[slot], 1);
  return 0;
}

// --- caption path retransmits --------------------------------------------
//
// Counted per component rather than per flow: the question the diagnosis needs
// answered is "was this component's egress lossy in the incident window", not
// "which connection".
SEC("tp_btf/tcp_retransmit_skb")
int BPF_PROG(on_tcp_retransmit, struct sock *sk, struct sk_buff *skb) {
  bump(&retransmits, bpf_get_current_cgroup_id());
  return 0;
}

// --- timing reference steps ----------------------------------------------
//
// A PTP grandmaster failover onto an NTP fallback shows up here before it shows
// up as caption drift. This is the hero incident's root cause, observed at the
// point where it actually happens.
SEC("tracepoint/syscalls/sys_enter_clock_adjtime")
int on_clock_adjtime(void *ctx) {
  bump(&clock_adjustments, bpf_get_current_cgroup_id());
  return 0;
}

// --- configuration reloads ------------------------------------------------
//
// `infra.stale_config` is the single most misdiagnosed fault in the benchmark
// (40 misdiagnoses). A component that has *not* reopened its configuration file
// since a change event was published is running stale config, and that is a
// negative signal no application metric exposes.
SEC("tracepoint/syscalls/sys_enter_openat")
int on_openat(struct trace_event_raw_sys_enter *ctx) {
  const char *pathname = (const char *)ctx->args[1];
  char path[64];
  if (bpf_probe_read_user_str(path, sizeof(path), pathname) < 0) return 0;

  // Only configuration paths. No general file-access telemetry is collected.
  if (!(path[0] == '/' && path[1] == 'e' && path[2] == 't' && path[3] == 'c'))
    return 0;

  bump(&config_reopens, bpf_get_current_cgroup_id());
  return 0;
}
