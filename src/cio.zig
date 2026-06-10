//! cio.zig — 0.16 stdlib compatibility shim.
//!
//! 0.16 removed std.fs.File.{stdout,stderr,stdin}, cio.Mutex/RwLock,
//! std.time.Timer, std.time.nanoTimestamp, std.process.Child.run, and
//! cio.posixGetenv. This shim wraps libc/pthread primitives on POSIX and
//! libc/kernel32 on native Windows so existing call sites continue to work
//! with minimal import-line changes.

const std = @import("std");
const builtin = @import("builtin");

const is_windows = builtin.os.tag == .windows;

pub const Fd = c_int;

extern "c" fn write(fd: c_int, ptr: [*]const u8, len: usize) isize;
extern "c" fn read(fd: c_int, ptr: [*]u8, len: usize) isize;
extern "c" fn isatty(fd: c_int) c_int;
extern "c" fn getenv(name: [*:0]const u8) ?[*:0]const u8;
extern "c" fn pipe(fds: *[2]c_int) c_int;
extern "c" fn close(fd: c_int) c_int;
extern "c" fn open(path: [*:0]const u8, oflag: c_int) c_int;
extern "c" fn getpid() c_int;
extern "c" fn getuid() c_uint;
extern "c" fn _write(fd: c_int, ptr: [*]const u8, len: c_uint) c_int;
extern "c" fn _read(fd: c_int, ptr: [*]u8, len: c_uint) c_int;
extern "c" fn _isatty(fd: c_int) c_int;
extern "c" fn _putenv_s(name: [*:0]const u8, value: [*:0]const u8) c_int;
const FileTime = extern struct { lo: u32, hi: u32 };
const PosixTimespec = extern struct { sec: isize, nsec: isize };
extern "c" fn clock_gettime(id: c_int, ts: *PosixTimespec) c_int;
extern "kernel32" fn GetSystemTimeAsFileTime(ft: *FileTime) callconv(.winapi) void;
extern "kernel32" fn AcquireSRWLockExclusive(lock: *?*anyopaque) callconv(.winapi) void;
extern "kernel32" fn ReleaseSRWLockExclusive(lock: *?*anyopaque) callconv(.winapi) void;
extern "kernel32" fn AcquireSRWLockShared(lock: *?*anyopaque) callconv(.winapi) void;
extern "kernel32" fn ReleaseSRWLockShared(lock: *?*anyopaque) callconv(.winapi) void;
extern "kernel32" fn TryAcquireSRWLockExclusive(lock: *?*anyopaque) callconv(.winapi) std.os.windows.BOOLEAN;
extern "kernel32" fn TryAcquireSRWLockShared(lock: *?*anyopaque) callconv(.winapi) std.os.windows.BOOLEAN;
extern "kernel32" fn QueryPerformanceCounter(value: *i64) callconv(.winapi) c_int;
extern "kernel32" fn QueryPerformanceFrequency(value: *i64) callconv(.winapi) c_int;
extern "kernel32" fn Sleep(ms: std.os.windows.DWORD) callconv(.winapi) void;

pub fn ignoreSigpipe() void {
    if (is_windows) return;
    var act: std.posix.Sigaction = .{
        .handler = .{ .handler = std.posix.SIG.IGN },
        .mask = std.posix.sigemptyset(),
        .flags = 0,
    };
    std.posix.sigaction(std.posix.SIG.PIPE, &act, null);
}

/// Detach a daemonized child from its controlling terminal: start a new
/// session (so it survives the spawning shell / parent CLI) and point
/// stdin/stdout/stderr at /dev/null (so it never holds the terminal open or
/// writes stray bytes to it). Best-effort — every step ignores errors, since a
/// failure here only means the daemon keeps an inherited fd, not that it
/// malfunctions. Called once at cli-daemon startup.
pub fn detachFromTerminal() void {
    if (is_windows) return;
    _ = std.c.setsid();
    // O_RDWR == 2 on both Darwin and Linux.
    const fd = open("/dev/null", 2);
    if (fd >= 0) {
        _ = std.c.dup2(fd, 0);
        _ = std.c.dup2(fd, 1);
        _ = std.c.dup2(fd, 2);
        if (fd > 2) _ = close(fd);
    }
}

// ── Stdio ────────────────────────────────────────────────────────────────

pub const File = struct {
    handle: Fd,

    pub fn stdout() File {
        return .{ .handle = 1 };
    }
    pub fn stderr() File {
        return .{ .handle = 2 };
    }
    pub fn stdin() File {
        return .{ .handle = 0 };
    }

    pub fn isTty(self: File) bool {
        if (is_windows) return _isatty(self.handle) != 0;
        return isatty(self.handle) != 0;
    }

    pub fn writeAll(self: File, data: []const u8) !void {
        var rem = data;
        while (rem.len > 0) {
            const n = writeRaw(self.handle, rem);
            if (n <= 0) return error.WriteFailed;
            rem = rem[@intCast(n)..];
        }
    }

    pub fn print(self: File, comptime fmt: []const u8, args: anytype) !void {
        var buf: [8192]u8 = undefined;
        const s = std.fmt.bufPrint(&buf, fmt, args) catch {
            const big = try std.fmt.allocPrint(std.heap.c_allocator, fmt, args);
            defer std.heap.c_allocator.free(big);
            return self.writeAll(big);
        };
        try self.writeAll(s);
    }
};

pub fn writeFd(fd: c_int, data: []const u8) void {
    (File{ .handle = fd }).writeAll(data) catch {};
}

fn writeRaw(fd: c_int, buf: []const u8) isize {
    if (is_windows) return _write(fd, buf.ptr, @intCast(@min(buf.len, std.math.maxInt(c_uint))));
    return write(fd, buf.ptr, buf.len);
}

fn readRaw(fd: c_int, buf: []u8) isize {
    if (is_windows) return _read(fd, buf.ptr, @intCast(@min(buf.len, std.math.maxInt(c_uint))));
    return read(fd, buf.ptr, buf.len);
}

pub fn processId() u64 {
    if (is_windows) return std.os.windows.GetCurrentProcessId();
    return @intCast(getpid());
}

pub fn userId() usize {
    if (is_windows) return 0;
    return @intCast(getuid());
}

pub fn mapFileRead(io: std.Io, allocator: std.mem.Allocator, file: std.Io.File, size: usize) ![]align(std.heap.page_size_min) const u8 {
    if (is_windows) {
        const buf = try allocator.alignedAlloc(u8, .fromByteUnits(std.heap.page_size_min), size);
        errdefer allocator.free(buf);
        if ((file.readPositionalAll(io, buf, 0) catch 0) != size) return error.ReadFailed;
        return buf;
    }
    return std.posix.mmap(null, size, .{ .READ = true }, .{ .TYPE = .SHARED }, file.handle, 0);
}

pub fn unmapFileRead(allocator: std.mem.Allocator, data: []align(std.heap.page_size_min) const u8) void {
    if (is_windows) {
        allocator.free(@constCast(data));
        return;
    }
    std.posix.munmap(data);
}

// ── Threads / Sync ───────────────────────────────────────────────────────

// Windows uses kernel32 SRWLOCK (zero-initialized pointer-sized lock) for both
// shapes; POSIX keeps the pthread primitives so concurrent readers stay free.
pub const Mutex = if (is_windows) struct {
    inner: ?*anyopaque = null,

    pub fn lock(self: *@This()) void {
        AcquireSRWLockExclusive(&self.inner);
    }
    pub fn unlock(self: *@This()) void {
        ReleaseSRWLockExclusive(&self.inner);
    }
    pub fn tryLock(self: *@This()) bool {
        return TryAcquireSRWLockExclusive(&self.inner) != 0;
    }
} else struct {
    inner: std.c.pthread_mutex_t = .{},

    pub fn lock(self: *@This()) void {
        _ = std.c.pthread_mutex_lock(&self.inner);
    }
    pub fn unlock(self: *@This()) void {
        _ = std.c.pthread_mutex_unlock(&self.inner);
    }
    pub fn tryLock(self: *@This()) bool {
        return std.c.pthread_mutex_trylock(&self.inner) == .SUCCESS;
    }
};

pub const RwLock = if (is_windows) struct {
    inner: ?*anyopaque = null,

    pub fn lock(self: *@This()) void {
        AcquireSRWLockExclusive(&self.inner);
    }
    pub fn unlock(self: *@This()) void {
        ReleaseSRWLockExclusive(&self.inner);
    }
    pub fn lockShared(self: *@This()) void {
        AcquireSRWLockShared(&self.inner);
    }
    pub fn unlockShared(self: *@This()) void {
        ReleaseSRWLockShared(&self.inner);
    }
    pub fn tryLock(self: *@This()) bool {
        return TryAcquireSRWLockExclusive(&self.inner) != 0;
    }
    pub fn tryLockShared(self: *@This()) bool {
        return TryAcquireSRWLockShared(&self.inner) != 0;
    }
} else struct {
    inner: std.c.pthread_rwlock_t = .{},

    pub fn lock(self: *@This()) void {
        _ = std.c.pthread_rwlock_wrlock(&self.inner);
    }
    pub fn unlock(self: *@This()) void {
        _ = std.c.pthread_rwlock_unlock(&self.inner);
    }
    pub fn lockShared(self: *@This()) void {
        _ = std.c.pthread_rwlock_rdlock(&self.inner);
    }
    pub fn unlockShared(self: *@This()) void {
        _ = std.c.pthread_rwlock_unlock(&self.inner);
    }
    pub fn tryLock(self: *@This()) bool {
        return std.c.pthread_rwlock_trywrlock(&self.inner) == .SUCCESS;
    }
    pub fn tryLockShared(self: *@This()) bool {
        return std.c.pthread_rwlock_tryrdlock(&self.inner) == .SUCCESS;
    }
};

// ── Time ─────────────────────────────────────────────────────────────────

pub fn nanoTimestamp() i128 {
    if (is_windows) {
        var ft: FileTime = undefined;
        GetSystemTimeAsFileTime(&ft);
        const hns = (@as(u64, ft.hi) << 32) | ft.lo;
        return (@as(i128, @intCast(hns)) - 116444736000000000) * 100;
    }
    var ts: PosixTimespec = undefined;
    _ = clock_gettime(0, &ts);
    return @as(i128, ts.sec) * 1_000_000_000 + ts.nsec;
}

pub fn milliTimestamp() i64 {
    return @intCast(@divTrunc(nanoTimestamp(), 1_000_000));
}

pub const Timer = struct {
    start_ns: i128,

    pub fn start() !Timer {
        return .{ .start_ns = monotonicNow() };
    }

    pub fn read(self: *Timer) u64 {
        return @intCast(monotonicNow() - self.start_ns);
    }

    pub fn lap(self: *Timer) u64 {
        const now = monotonicNow();
        const delta: u64 = @intCast(now - self.start_ns);
        self.start_ns = now;
        return delta;
    }
};

// The performance-counter frequency is fixed at boot, so query it once and
// cache it. Concurrent first calls race benignly: every writer stores the
// same value.
var qpf_cache = std.atomic.Value(i64).init(0);

fn monotonicNow() i128 {
    if (is_windows) {
        var freq = qpf_cache.load(.monotonic);
        if (freq <= 0) {
            if (QueryPerformanceFrequency(&freq) == 0 or freq <= 0) return nanoTimestamp();
            qpf_cache.store(freq, .monotonic);
        }
        var counter: i64 = 0;
        if (QueryPerformanceCounter(&counter) == 0) return nanoTimestamp();
        return @divTrunc(@as(i128, counter) * 1_000_000_000, freq);
    }
    var ts: PosixTimespec = undefined;
    _ = clock_gettime(if (builtin.os.tag == .macos) 6 else 1, &ts);
    return @as(i128, ts.sec) * 1_000_000_000 + ts.nsec;
}

// ── Environment ──────────────────────────────────────────────────────────

/// Non-cryptographic random u64 mixing nanotime, PID, and thread ID.
/// Replaces `std.crypto.random.int(u64)` (removed in 0.16) for tmp-file
/// suffix collision avoidance. Thread-safe: each thread gets a unique
/// mix per-call even at the same nanosecond.
pub fn randU64() u64 {
    const now = nanoTimestamp();
    const ns = @as(u64, @intCast(@mod(now, 1_000_000_000)));
    const sec = @as(u64, @intCast(@divTrunc(now, 1_000_000_000)));
    const tid = std.Thread.getCurrentId();
    const pid: u64 = processId();
    // splitmix64-style final mixing to avoid close-timestamp collisions
    var x = ns ^ (sec *% 2) ^ (tid *% (1 << 17)) ^ (pid *% (1 << 23));
    x ^= x >> 33;
    x *%= 0xff51afd7ed558ccd;
    x ^= x >> 33;
    x *%= 0xc4ceb9fe1a85ec53;
    x ^= x >> 33;
    return x;
}

pub fn sleepMs(ms: u64) void {
    if (is_windows) {
        Sleep(@intCast(@min(ms, std.math.maxInt(std.os.windows.DWORD))));
        return;
    }
    var ts: std.c.timespec = .{
        .sec = @intCast(ms / 1000),
        .nsec = @intCast((ms % 1000) * 1_000_000),
    };
    _ = std.c.nanosleep(&ts, null);
}

pub const PipeError = error{PipeFailed};
pub fn makePipe() PipeError![2]c_int {
    if (is_windows) return error.PipeFailed;
    var fds: [2]c_int = .{ -1, -1 };
    if (pipe(&fds) != 0) return error.PipeFailed;
    return fds;
}

pub fn closeFd(fd: c_int) void {
    if (is_windows) return;
    _ = close(fd);
}

pub fn posixGetenv(name: []const u8) ?[]const u8 {
    return getenvZ(name);
}

/// Per-user home directory for the central codedb cache: HOME on POSIX,
/// HOME-or-USERPROFILE on native Windows. All HOME readers must go through
/// this so the Windows fallback lives in exactly one place.
pub fn userHome() ?[]const u8 {
    if (getenvZ("HOME")) |home| return home;
    if (is_windows) return getenvZ("USERPROFILE");
    return null;
}

/// NUL-terminate `s` into `buf` for a libc call. Null if it doesn't fit.
fn zTerm(buf: []u8, s: []const u8) ?[*:0]const u8 {
    if (s.len >= buf.len) return null;
    @memcpy(buf[0..s.len], s);
    buf[s.len] = 0;
    return @ptrCast(buf.ptr);
}

fn getenvZ(name: []const u8) ?[]const u8 {
    var buf: [256]u8 = undefined;
    const name_z = zTerm(&buf, name) orelse return null;
    const ptr = getenv(name_z) orelse return null;
    return std.mem.span(ptr);
}

extern "c" fn setenv(name: [*:0]const u8, value: [*:0]const u8, overwrite: c_int) c_int;
extern "c" fn unsetenv(name: [*:0]const u8) c_int;

/// Set an environment variable (libc setenv), visible to subsequent posixGetenv
/// reads in this process. Names ≥256 / values ≥4096 bytes are ignored. Used by
/// CLI opt-in switches that flip in-process policy — e.g. `--allow-temp` sets
/// CODEDB_ALLOW_TEMP (#538) — and by tests.
pub fn posixSetenv(name: []const u8, value: []const u8) void {
    var nbuf: [256]u8 = undefined;
    var vbuf: [4096]u8 = undefined;
    const name_z = zTerm(&nbuf, name) orelse return;
    const value_z = zTerm(&vbuf, value) orelse return;
    if (is_windows) {
        _ = _putenv_s(name_z, value_z);
    } else {
        _ = setenv(name_z, value_z, 1);
    }
}

/// Remove an environment variable (libc unsetenv).
pub fn posixUnsetenv(name: []const u8) void {
    var nbuf: [256]u8 = undefined;
    const name_z = zTerm(&nbuf, name) orelse return;
    if (is_windows) {
        _ = _putenv_s(name_z, "");
    } else {
        _ = unsetenv(name_z);
    }
}

/// Read one line from stdin (fd 0) into `buf`, trimming trailing CR/LF. Returns
/// null on EOF/error. For interactive CLI prompts only — NEVER call this in the
/// MCP server path, where stdin is the JSON-RPC transport.
pub fn readLine(buf: []u8) ?[]const u8 {
    const n = readRaw(0, buf);
    if (n <= 0) return null;
    return std.mem.trimEnd(u8, buf[0..@intCast(n)], "\r\n");
}

// ── Arguments ────────────────────────────────────────────────────────────

// Darwin: argv lives in __NSGetArgv() (libc, from <crt_externs.h>).
// Linux/other POSIX: 0.16 doesn't expose argv globally — main() must call
// `setProcessArgs(argv_slice)` once at startup to populate `process_args`.
extern "c" fn _NSGetArgc() *c_int;
extern "c" fn _NSGetArgv() *[*][*:0]u8;

var process_args: ?[]const [*:0]const u8 = null;

/// Called once by `pub fn main` to register the argv slice on non-Darwin
/// platforms. No-op on macOS (it reads from `_NSGetArgv` directly).
pub fn setProcessArgs(args: []const [*:0]const u8) void {
    process_args = args;
}

/// Windows delivers the command line as WTF-16. `Args.toSlice` converts it to
/// WTF-8 null-terminated strings; flatten those into the posix-shaped C-string
/// argv the rest of the code uses. Allocated from the c allocator and
/// intentionally leaked for the process lifetime.
pub fn windowsArgv(args: std.process.Args) ![]const [*:0]const u8 {
    const a = std.heap.c_allocator;
    const slices = try args.toSlice(a);
    const out = try a.alloc([*:0]const u8, slices.len);
    for (slices, 0..) |s, i| out[i] = s.ptr;
    return out;
}

/// Shim for cio.argsAlloc (removed in 0.16). Returns a duplicated
/// slice of argv strings owned by the allocator; free with argsFree.
pub fn argsAlloc(alloc: std.mem.Allocator) ![][:0]u8 {
    const argc: usize = if (builtin.os.tag == .macos)
        @intCast(_NSGetArgc().*)
    else
        (process_args orelse return error.ProcessArgsNotSet).len;
    const out = try alloc.alloc([:0]u8, argc);
    errdefer alloc.free(out);
    var filled: usize = 0;
    errdefer {
        var i: usize = 0;
        while (i < filled) : (i += 1) alloc.free(out[i]);
    }
    while (filled < argc) : (filled += 1) {
        const cstr: [*:0]const u8 = if (builtin.os.tag == .macos)
            _NSGetArgv().*[filled]
        else
            process_args.?[filled];
        const s = std.mem.span(cstr);
        const dup = try alloc.allocSentinel(u8, s.len, 0);
        @memcpy(dup[0..s.len], s);
        out[filled] = dup;
    }
    return out;
}

pub fn argsFree(alloc: std.mem.Allocator, args: [][:0]u8) void {
    for (args) |a| alloc.free(a);
    alloc.free(args);
}

// ── ArrayList writer helper (replaces 0.15's ArrayList(u8).writer(alloc)) ────

pub const ListWriter = struct {
    list: *std.ArrayList(u8),
    alloc: std.mem.Allocator,

    pub fn writeAll(self: ListWriter, bytes: []const u8) !void {
        try self.list.appendSlice(self.alloc, bytes);
    }
    pub fn writeByte(self: ListWriter, b: u8) !void {
        try self.list.append(self.alloc, b);
    }
    pub fn writeByteNTimes(self: ListWriter, b: u8, n: usize) !void {
        try self.list.appendNTimes(self.alloc, b, n);
    }
    pub fn writeBytesNTimes(self: ListWriter, bytes: []const u8, n: usize) !void {
        var i: usize = 0;
        while (i < n) : (i += 1) try self.list.appendSlice(self.alloc, bytes);
    }
    pub fn print(self: ListWriter, comptime fmt: []const u8, args: anytype) !void {
        var stack_buf: [8192]u8 = undefined;
        const s = std.fmt.bufPrint(&stack_buf, fmt, args) catch {
            const big = try std.fmt.allocPrint(self.alloc, fmt, args);
            defer self.alloc.free(big);
            try self.list.appendSlice(self.alloc, big);
            return;
        };
        try self.list.appendSlice(self.alloc, s);
    }
};

pub fn listWriter(list: *std.ArrayList(u8), alloc: std.mem.Allocator) ListWriter {
    return .{ .list = list, .alloc = alloc };
}

// ── Subprocess ───────────────────────────────────────────────────────────

pub const CaptureResult = struct {
    stdout: []u8,
    stderr: []u8,
    term: Term,

    pub const Term = union(enum) {
        Exited: u8,
        Signal: u32,
        Stopped: u32,
        Unknown: u32,
    };
};

pub const RunOptions = struct {
    allocator: std.mem.Allocator,
    argv: []const []const u8,
    cwd: ?[]const u8 = null,
    max_output_bytes: usize = 50 * 1024 * 1024,
};
extern "c" fn _NSGetEnviron() *[*:null]?[*:0]u8;

// posix_spawn family — declared directly via extern "c" so this builds on
// both Darwin and Linux. (std.c.posix_spawnp is gated to .isDarwin() in 0.16
// and would fail to compile on Linux even though glibc/musl provide it.)
const PosixSpawnFileActions = opaque {};
const PosixSpawnAttr = opaque {};
const pid_t = c_int;

extern "c" fn posix_spawnp(
    pid: *pid_t,
    path: [*:0]const u8,
    file_actions: ?*const PosixSpawnFileActions,
    attrp: ?*const PosixSpawnAttr,
    argv: [*:null]const ?[*:0]const u8,
    envp: [*:null]const ?[*:0]const u8,
) c_int;
extern "c" fn posix_spawn_file_actions_init(fa: *PosixSpawnFileActions) c_int;
extern "c" fn posix_spawn_file_actions_destroy(fa: *PosixSpawnFileActions) c_int;
extern "c" fn posix_spawn_file_actions_adddup2(fa: *PosixSpawnFileActions, fd: c_int, newfd: c_int) c_int;
extern "c" fn posix_spawn_file_actions_addclose(fa: *PosixSpawnFileActions, fd: c_int) c_int;
extern "c" fn posix_spawn_file_actions_addchdir_np(fa: *PosixSpawnFileActions, path: [*:0]const u8) c_int;
extern "c" fn posix_spawn_file_actions_addopen(fa: *PosixSpawnFileActions, fd: c_int, path: [*:0]const u8, oflag: c_int, mode: c_uint) c_int;
extern "c" fn waitpid(pid: pid_t, status: *c_int, options: c_int) pid_t;

// posix_spawn_file_actions_t is a struct of unknown size on each libc. We
// allocate a generously-sized buffer and cast to the opaque pointer type.
const PosixSpawnFAStorage = [256]u8;

/// Shim for std.process.Child.run — fast posix_spawnp path.
/// Captures stdout and stderr into separate streams (drained concurrently by
/// a background thread to avoid pipe-buffer deadlock when the child writes
/// substantially to either stream).
pub fn runCapture(opts: RunOptions) !CaptureResult {
    if (is_windows) return runCaptureWindows(opts);
    return runCapturePosix(opts);
}

fn runCaptureWindows(opts: RunOptions) !CaptureResult {
    const alloc = opts.allocator;
    var threaded: std.Io.Threaded = .init(alloc, .{});
    defer threaded.deinit();
    const io = threaded.io();
    const result = try std.process.run(alloc, io, .{
        .argv = opts.argv,
        .cwd = if (opts.cwd) |cwd| .{ .path = cwd } else .inherit,
        // Match the POSIX capture path: subprocess probes are bounded so noisy
        // tools cannot turn a small git/status check into unbounded memory use.
        .stdout_limit = .limited(opts.max_output_bytes),
        .stderr_limit = .limited(opts.max_output_bytes),
    });
    const term: CaptureResult.Term = switch (result.term) {
        .exited => |code| .{ .Exited = code },
        .signal => |sig| .{ .Signal = @intFromEnum(sig) },
        .stopped => |sig| .{ .Stopped = @intFromEnum(sig) },
        .unknown => |code| .{ .Unknown = code },
    };
    return .{
        .stdout = result.stdout,
        .stderr = result.stderr,
        .term = term,
    };
}

fn runCapturePosix(opts: RunOptions) !CaptureResult {
    if (opts.argv.len == 0) return error.EmptyArgv;
    const alloc = opts.allocator;

    const c_argv = try alloc.alloc(?[*:0]const u8, opts.argv.len + 1);
    defer alloc.free(c_argv);
    const arg_bufs = try alloc.alloc([]u8, opts.argv.len);
    defer {
        for (arg_bufs) |b| alloc.free(b);
        alloc.free(arg_bufs);
    }
    for (opts.argv, 0..) |a, i| {
        const buf = try alloc.alloc(u8, a.len + 1);
        @memcpy(buf[0..a.len], a);
        buf[a.len] = 0;
        arg_bufs[i] = buf;
        c_argv[i] = @ptrCast(buf.ptr);
    }
    c_argv[opts.argv.len] = null;
    const c_argv_z: [*:null]const ?[*:0]const u8 = @ptrCast(c_argv.ptr);

    var out_pipe: [2]c_int = .{ -1, -1 };
    var err_pipe: [2]c_int = .{ -1, -1 };
    if (pipe(&out_pipe) != 0) return error.PipeFailed;
    errdefer {
        if (out_pipe[0] >= 0) _ = close(out_pipe[0]);
        if (out_pipe[1] >= 0) _ = close(out_pipe[1]);
    }
    if (pipe(&err_pipe) != 0) return error.PipeFailed;
    errdefer {
        if (err_pipe[0] >= 0) _ = close(err_pipe[0]);
        if (err_pipe[1] >= 0) _ = close(err_pipe[1]);
    }

    var fa_storage: PosixSpawnFAStorage = undefined;
    const fa: *PosixSpawnFileActions = @ptrCast(&fa_storage);
    if (posix_spawn_file_actions_init(fa) != 0) return error.SpawnInitFailed;
    defer _ = posix_spawn_file_actions_destroy(fa);

    if (opts.cwd) |cwd| {
        var cwd_buf: [4096]u8 = undefined;
        if (cwd.len >= cwd_buf.len) return error.PathTooLong;
        @memcpy(cwd_buf[0..cwd.len], cwd);
        cwd_buf[cwd.len] = 0;
        // posix_spawn_file_actions_addchdir_np is glibc 2.29+ / macOS 10.15+.
        // Returns ENOSYS on older systems — caller treats that as fatal here.
        if (posix_spawn_file_actions_addchdir_np(fa, @ptrCast(&cwd_buf)) != 0) {
            return error.CwdNotSupported;
        }
    }

    _ = posix_spawn_file_actions_adddup2(fa, out_pipe[1], 1);
    _ = posix_spawn_file_actions_adddup2(fa, err_pipe[1], 2);
    _ = posix_spawn_file_actions_addclose(fa, out_pipe[0]);
    _ = posix_spawn_file_actions_addclose(fa, out_pipe[1]);
    _ = posix_spawn_file_actions_addclose(fa, err_pipe[0]);
    _ = posix_spawn_file_actions_addclose(fa, err_pipe[1]);

    const envp: [*:null]const ?[*:0]const u8 = if (builtin.os.tag == .macos)
        @ptrCast(_NSGetEnviron().*)
    else
        @ptrCast(std.c.environ);

    var pid: pid_t = 0;
    if (posix_spawnp(&pid, c_argv[0].?, fa, null, c_argv_z, envp) != 0)
        return error.SpawnFailed;

    _ = close(out_pipe[1]);
    out_pipe[1] = -1;
    _ = close(err_pipe[1]);
    err_pipe[1] = -1;

    // Drain stderr on a background thread so neither pipe can fill up and
    // deadlock the child. Main thread drains stdout.
    const DrainCtx = struct {
        fd: c_int,
        cap: usize,
        alloc: std.mem.Allocator,
        out: std.ArrayList(u8) = .empty,
        err: ?anyerror = null,

        fn run(self: *@This()) void {
            var chunk: [64 * 1024]u8 = undefined;
            while (self.out.items.len < self.cap) {
                const want = @min(chunk.len, self.cap - self.out.items.len);
                const n = read(self.fd, &chunk, want);
                if (n <= 0) break;
                self.out.appendSlice(self.alloc, chunk[0..@intCast(n)]) catch |e| {
                    self.err = e;
                    return;
                };
            }
        }
    };
    var err_ctx: DrainCtx = .{ .fd = err_pipe[0], .cap = opts.max_output_bytes, .alloc = alloc };
    errdefer err_ctx.out.deinit(alloc);
    const err_thread = try std.Thread.spawn(.{}, DrainCtx.run, .{&err_ctx});

    var out: std.ArrayList(u8) = .empty;
    errdefer out.deinit(alloc);
    var chunk: [64 * 1024]u8 = undefined;
    while (out.items.len < opts.max_output_bytes) {
        const want = @min(chunk.len, opts.max_output_bytes - out.items.len);
        const n = read(out_pipe[0], &chunk, want);
        if (n <= 0) break;
        try out.appendSlice(alloc, chunk[0..@intCast(n)]);
    }
    _ = close(out_pipe[0]);
    out_pipe[0] = -1;

    err_thread.join();
    _ = close(err_pipe[0]);
    err_pipe[0] = -1;
    if (err_ctx.err) |e| {
        err_ctx.out.deinit(alloc);
        return e;
    }

    var status: c_int = 0;
    _ = waitpid(pid, &status, 0);

    const term: CaptureResult.Term = if ((status & 0x7f) == 0)
        .{ .Exited = @intCast((status >> 8) & 0xff) }
    else if ((status & 0x7f) != 0x7f)
        .{ .Signal = @intCast(status & 0x7f) }
    else
        .{ .Stopped = @intCast((status >> 8) & 0xff) };

    return .{
        .stdout = try out.toOwnedSlice(alloc),
        .stderr = try err_ctx.out.toOwnedSlice(alloc),
        .term = term,
    };
}

/// Fire-and-forget spawn: posix_spawnp `argv` with stdin/stdout/stderr
/// redirected to /dev/null, and do NOT wait on the child. Used to launch the
/// warm cli-daemon from a cold CLI invocation. The child is expected to
/// setsid() itself; once this CLI process exits the child is reparented to
/// init, so we never reap it (no zombie outlives this short-lived CLI). All
/// failures are swallowed — auto-spawn is best-effort and the cold path still
/// produces correct output regardless.
pub fn spawnDetached(allocator: std.mem.Allocator, argv: []const []const u8) void {
    if (is_windows) {
        spawnDetachedWindows(allocator, argv);
        return;
    }
    spawnDetachedPosix(allocator, argv);
}

/// Build a CreateProcessW command line from argv (each arg quoted). Callers
/// spawn children that split it back with CommandLineToArgvW-compatible rules.
/// Caller owns the returned slice; null on allocation failure.
pub fn windowsCommandLine(allocator: std.mem.Allocator, argv: []const []const u8) ?[]u8 {
    var cmd: std.ArrayList(u8) = .empty;
    defer cmd.deinit(allocator);
    for (argv, 0..) |arg, i| {
        if (i != 0) cmd.append(allocator, ' ') catch return null;
        cmd.append(allocator, '"') catch return null;
        for (arg) |ch| {
            if (ch == '"') cmd.append(allocator, '\\') catch return null;
            cmd.append(allocator, ch) catch return null;
        }
        cmd.append(allocator, '"') catch return null;
    }
    return cmd.toOwnedSlice(allocator) catch null;
}

fn spawnDetachedWindows(allocator: std.mem.Allocator, argv: []const []const u8) void {
    if (argv.len == 0) return;

    const cmd = windowsCommandLine(allocator, argv) orelse return;
    defer allocator.free(cmd);
    const cmd_w = std.unicode.utf8ToUtf16LeAllocZ(allocator, cmd) catch return;
    defer allocator.free(cmd_w);

    var si: std.os.windows.STARTUPINFOW = std.mem.zeroes(std.os.windows.STARTUPINFOW);
    si.cb = @sizeOf(std.os.windows.STARTUPINFOW);
    var pi: std.os.windows.PROCESS.INFORMATION = undefined;
    // Windows has no setsid/dev-null equivalent for this use case. DETACHED
    // PROCESS + CREATE_NO_WINDOW gives the same user-visible behavior: the
    // warm cli-daemon outlives the spawning CLI and does not flash a console.
    // Use the wide API so self_exe/root paths outside the active ANSI code page
    // still launch correctly from non-ASCII Windows profiles/checkouts.
    const flags: std.os.windows.CreateProcessFlags = .{ .detached_process = true, .create_no_window = true };
    if (std.os.windows.kernel32.CreateProcessW(null, cmd_w.ptr, null, null, .FALSE, flags, null, null, &si, &pi) == .FALSE) return;
    std.os.windows.CloseHandle(pi.hThread);
    std.os.windows.CloseHandle(pi.hProcess);
}

fn spawnDetachedPosix(allocator: std.mem.Allocator, argv: []const []const u8) void {
    if (argv.len == 0) return;

    const c_argv = allocator.alloc(?[*:0]const u8, argv.len + 1) catch return;
    defer allocator.free(c_argv);
    const arg_bufs = allocator.alloc([]u8, argv.len) catch return;
    var built: usize = 0;
    defer {
        for (arg_bufs[0..built]) |b| allocator.free(b);
        allocator.free(arg_bufs);
    }
    for (argv, 0..) |a, i| {
        const buf = allocator.alloc(u8, a.len + 1) catch return;
        @memcpy(buf[0..a.len], a);
        buf[a.len] = 0;
        arg_bufs[i] = buf;
        built = i + 1;
        c_argv[i] = @ptrCast(buf.ptr);
    }
    c_argv[argv.len] = null;
    const c_argv_z: [*:null]const ?[*:0]const u8 = @ptrCast(c_argv.ptr);

    var fa_storage: PosixSpawnFAStorage = undefined;
    const fa: *PosixSpawnFileActions = @ptrCast(&fa_storage);
    if (posix_spawn_file_actions_init(fa) != 0) return;
    defer _ = posix_spawn_file_actions_destroy(fa);

    // Redirect 0/1/2 to /dev/null so the daemon holds no inherited terminal fds.
    // O_RDWR == 2 on both Darwin and Linux. Errors are non-fatal — the daemon
    // re-redirects to /dev/null itself after setsid() at startup.
    const devnull: [*:0]const u8 = "/dev/null";
    _ = posix_spawn_file_actions_addopen(fa, 0, devnull, 2, 0);
    _ = posix_spawn_file_actions_addopen(fa, 1, devnull, 2, 0);
    _ = posix_spawn_file_actions_addopen(fa, 2, devnull, 2, 0);

    const envp: [*:null]const ?[*:0]const u8 = if (builtin.os.tag == .macos)
        @ptrCast(_NSGetEnviron().*)
    else
        @ptrCast(std.c.environ);

    var pid: pid_t = 0;
    // Fire and forget: no waitpid. The child reparents to init once this CLI
    // exits, so it never becomes a lingering zombie of ours.
    _ = posix_spawnp(&pid, c_argv[0].?, fa, null, c_argv_z, envp);
}
