const std = @import("std");
const cio = @import("cio.zig");

fn isExactOrChild(path: []const u8, prefix: []const u8) bool {
    if (!std.mem.startsWith(u8, path, prefix)) return false;
    return path.len == prefix.len or path[prefix.len] == '/';
}

fn isPathSep(c: u8) bool {
    return c == '/' or c == '\\';
}

fn matchPathSegment(path: []const u8, index: *usize, segment: []const u8) bool {
    const start = index.*;
    const end = start + segment.len;
    if (end > path.len) return false;
    if (!std.ascii.eqlIgnoreCase(path[start..end], segment)) return false;
    if (end == path.len) {
        index.* = end;
        return true;
    }
    if (!isPathSep(path[end])) return false;
    index.* = end + 1;
    return true;
}

fn consumeAnyPathSegment(path: []const u8, index: *usize) bool {
    const start = index.*;
    var end = start;
    while (end < path.len and !isPathSep(path[end])) : (end += 1) {}
    if (end == start or end == path.len) return false;
    index.* = end + 1;
    return true;
}

fn isNativeWindowsTempRoot(path: []const u8) bool {
    if (path.len < 3 or path[1] != ':' or !std.ascii.isAlphabetic(path[0]) or !isPathSep(path[2])) return false;

    var windows_idx: usize = 3;
    if (matchPathSegment(path, &windows_idx, "Windows") and
        matchPathSegment(path, &windows_idx, "Temp"))
    {
        return true;
    }

    var user_idx: usize = 3;
    if (matchPathSegment(path, &user_idx, "Users") and
        consumeAnyPathSegment(path, &user_idx) and
        matchPathSegment(path, &user_idx, "AppData") and
        matchPathSegment(path, &user_idx, "Local") and
        matchPathSegment(path, &user_idx, "Temp"))
    {
        return true;
    }

    return false;
}

/// Temp-root indexing is an opt-in escape hatch for CI / SWE-bench harnesses
/// that clone throwaway checkouts under /tmp. Off by default (footgun guard,
/// #80/#346). Enabled by CODEDB_ALLOW_TEMP=1; the `--allow-temp` CLI flag sets
/// that env so both opt-ins share one switch. See #538.
pub fn tempIndexingAllowed() bool {
    const v = cio.posixGetenv("CODEDB_ALLOW_TEMP") orelse return false;
    return v.len > 0 and !std.mem.eql(u8, v, "0");
}

pub fn isIndexableRoot(path: []const u8) bool {
    if (path.len == 0) return false;
    if (std.mem.eql(u8, path, "/")) return false;
    // /tmp and /private/tmp are refused by default (footgun guard) but allowed
    // when temp indexing is opted in (#538) — CI/SWE-bench harnesses clone into /tmp.
    if (!tempIndexingAllowed()) {
        if (isExactOrChild(path, "/private/tmp")) return false;
        if (isExactOrChild(path, "/tmp")) return false;
        if (isNativeWindowsTempRoot(path)) return false;
    }

    const system_prefixes = [_][]const u8{
        "/Applications",
        "/System",
        "/Library",
        "/usr",
        "/opt",
        "/bin",
        "/sbin",
        "/etc",
        "/private/etc",
        "/dev",
        "/proc",
        "/sys",
        "/snap",
        "/nix",
        "/var",
        "/private/var",
    };
    for (system_prefixes) |pfx| {
        if (isExactOrChild(path, pfx)) return false;
    }

    // Block home directory itself (not subdirectories) — prevents 17GB RAM spike (#174)
    if (cio.userHome()) |home| {
        if (home.len > 0 and std.mem.eql(u8, path, home)) return false;
    }
    // Also block common home patterns directly
    if (std.mem.eql(u8, path, "/root")) return false;
    if (std.mem.startsWith(u8, path, "/home/") or std.mem.startsWith(u8, path, "/Users/")) {
        // /home/user or /Users/user (no deeper path component) = home dir
        const rest = if (std.mem.startsWith(u8, path, "/home/")) path[6..] else path[7..];
        if (std.mem.indexOfScalar(u8, rest, '/') == null and rest.len > 0) return false;
    }

    return true;
}

const testing = std.testing;

test "issue-80: normal paths are allowed" {
    try testing.expect(isIndexableRoot("/Users/dev/project"));
    try testing.expect(isIndexableRoot("/home/user/code"));
    try testing.expect(isIndexableRoot("/home/user/code/subdir"));
}

test "issue-174: home directory itself is denied" {
    try testing.expect(!isIndexableRoot("/root"));
    try testing.expect(!isIndexableRoot("/home/user"));
    try testing.expect(!isIndexableRoot("/Users/dev"));
    // But subdirectories are allowed
    try testing.expect(isIndexableRoot("/home/user/projects"));
    try testing.expect(isIndexableRoot("/Users/dev/code"));
    try testing.expect(isIndexableRoot("/root/projects"));
}
test "issue-80: empty path is denied" {
    try testing.expect(!isIndexableRoot(""));
}

test "issue-80: /tmp is denied" {
    try testing.expect(!isIndexableRoot("/tmp"));
    try testing.expect(!isIndexableRoot("/tmp/foo"));
}

test "issue-538: native Windows temp roots are denied" {
    try testing.expect(!isIndexableRoot("C:\\Users\\dev\\AppData\\Local\\Temp"));
    try testing.expect(!isIndexableRoot("C:\\Users\\dev\\AppData\\Local\\Temp\\repo"));
    try testing.expect(!isIndexableRoot("C:/Users/dev/AppData/Local/Temp/repo"));
    try testing.expect(!isIndexableRoot("C:\\Windows\\Temp\\repo"));
    try testing.expect(isIndexableRoot("C:\\Users\\dev\\projects\\repo"));
}
