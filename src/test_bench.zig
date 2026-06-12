const std = @import("std");
const explore = @import("explore.zig");
const cio = @import("cio.zig");

test "bench: fuzzyScore throughput" {
    const paths = [_][]const u8{
        "src/components/authentication/LoginForm.tsx",
        "packages/core/lib/utils/string-helpers.rs",
        "internal/server/middleware/rate_limiter.go",
        "src/database/migrations/20240301_add_users.sql",
        "frontend/src/hooks/useAuth.ts",
        "very/deeply/nested/project/structure/with/many/levels/file.zig",
    };
    const queries = [_][]const u8{
        "LoginForm",
        "string",
        "rate_limiter",
        "useAuth",
        "file.zig",
        "migrat",
    };

    const iterations: usize = 50_000;
    const t0 = cio.nanoTimestamp();

    for (0..iterations) |i| {
        for (queries) |q| {
            std.mem.doNotOptimizeAway(explore.fuzzyScore(q, paths[i % paths.len]));
        }
    }

    const elapsed: u64 = @intCast(cio.nanoTimestamp() - t0);
    const total_calls = iterations * queries.len;
    const per_call_ns = elapsed / total_calls;
    if (cio.posixGetenv("CODEDB_BENCH_VERBOSE") != null) std.debug.print("\n  fuzzyScore: {d} calls in {d}ms ({d}ns/call)\n", .{
        total_calls,
        elapsed / std.time.ns_per_ms,
        per_call_ns,
    });
}

test "bench: detectLanguage + isDocLanguage" {
    const paths = [_][]const u8{
        "src/main.zig",
        "lib/utils.rs",
        "docs/README.md",
        "config.json",
        "app.py",
        "index.tsx",
        "style.scss",
        "Makefile",
        "server.go",
        "test.cpp",
    };

    const iterations: usize = 200_000;
    const t0 = cio.nanoTimestamp();
    var doc_count: usize = 0;

    for (0..iterations) |i| {
        const lang = explore.detectLanguage(paths[i % paths.len]);
        if (explore.isDocLanguage(lang)) doc_count += 1;
    }

    const elapsed: u64 = @intCast(cio.nanoTimestamp() - t0);
    if (cio.posixGetenv("CODEDB_BENCH_VERBOSE") != null) std.debug.print("\n  detectLanguage+isDocLanguage: {d} calls in {d}ms ({d}ns/call, {d} docs)\n", .{
        iterations,
        elapsed / std.time.ns_per_ms,
        elapsed / iterations,
        doc_count,
    });
}
