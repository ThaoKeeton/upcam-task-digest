using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Upcam.Workbench;

// This executable is also the offline worker fixture. Tests run real Windows
// processes, pipes, Job Objects and the same named singleton as standalone.
internal static class Program
{
    private const string Singleton = @"Local\UPCAMTaskDigest.Singleton.v1";
    private const string PrivateMarker = "SYNTHETIC_SECRET_MUST_NOT_ESCAPE";

    private static async Task<int> Main(string[] args)
    {
        if (args.Contains("--data-directory")) return await RunFixtureAsync(args);
        if (args.FirstOrDefault() == "--descendant")
        {
            AtomicMarker(args[1], Environment.ProcessId.ToString());
            await Task.Delay(Timeout.Infinite);
            return 0;
        }
        if (args.FirstOrDefault() == "--crash-host")
        {
            await using var client = await PluginWorkerClient.StartAsync(args[1], args[2], true, CancellationToken.None);
            await client.SendAsync("spawn_descendant", null, CancellationToken.None);
            await File.WriteAllTextAsync(Path.Combine(args[2], "host-ready"), "ready");
            await Task.Delay(Timeout.Infinite);
            return 0;
        }
        if (!OperatingSystem.IsWindows())
        {
            Console.Error.WriteLine("Transport process tests require Windows; no tests ran.");
            return 2;
        }

        (string Name, Func<Task> Run)[] tests =
        [
            ("handshake, explicit profile and package paths containing spaces", ValidStart),
            ("synthetic mode is omitted when not explicitly requested", NoSyntheticFlag),
            ("a fresh explicit profile may be created by the worker", NewProfile),
            ("short aliases bind the same long profile and reject a different profile", ShortLongAliases),
            ("all handshake fields are bound to the requested session", BadHandshakes),
            ("a standalone singleton prevents worker creation", OccupiedSingleton),
            ("racing starts permit one backend session", RacingStarts),
            ("relative, UNC and root paths are rejected before spawn", UnsafePaths),
            ("package and profile reparse components are rejected", ReparsePaths),
            ("worker failures never expose protocol or stderr", SafeErrors),
            ("UTF-8, JSON, correlation and framing failures terminate the worker", BadResponses),
            ("response bytes above 32 MiB are rejected", OversizedResponse),
            ("requests above 2 MiB never reach the worker", OversizedRequest),
            ("concurrent callers produce serialized correlated requests", SerializedCalls),
            ("large stderr cannot block a response", DiscardStderr),
            ("startup cancellation kills the worker and releases the singleton", CancelStartup),
            ("session lifetime cancellation stops an idle worker", CancelLifetime),
            ("a pre-cancelled request closes only its owned session", CancelBeforeRequest),
            ("request cancellation kills without replay and preserves uncertainty", CancelSend),
            ("ordinary requests time out after 30 seconds without replay", RequestDeadline),
            ("diagnosis, save and authorized managed runs may exceed 30 seconds", CollectionDeadline),
            ("dispose interrupts an in-flight operation and never replays", DisposeBusy),
            ("shutdown completes before singleton release and is idempotent", Shutdown),
            ("unresponsive shutdown is bounded and releases after termination", ShutdownDeadline),
            ("Job Object kills worker descendants when the host dies", HostDeath)
        ];
        var results = new JsonArray();
        int failed = 0;
        foreach (var test in tests)
        {
            var elapsed = Stopwatch.StartNew();
            string? error = null;
            try { await test.Run(); }
            catch (Exception exception) { failed++; error = exception.GetType().Name + ": " + exception.Message; }
            Console.WriteLine($"{(error is null ? "PASS" : "FAIL")} {test.Name}{(error is null ? "" : ": " + error)}");
            results.Add(new JsonObject { ["name"] = test.Name, ["passed"] = error is null,
                ["seconds"] = elapsed.Elapsed.TotalSeconds, ["error"] = error });
        }
        int reportIndex = Array.IndexOf(args, "--report");
        if (reportIndex >= 0 && reportIndex + 1 < args.Length)
            await File.WriteAllTextAsync(args[reportIndex + 1], new JsonObject
            { ["platform"] = "Windows", ["passed"] = tests.Length - failed, ["failed"] = failed,
                ["tests"] = results }.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine($"{tests.Length - failed}/{tests.Length} transport process tests passed.");
        return failed == 0 ? 0 : 1;
    }

    private static async Task ValidStart()
    {
        using var fixture = new Fixture();
        await using var client = await fixture.Start();
        Check(client.IsUsable, "The validated session is unavailable.");
        JsonObject echo = await client.SendAsync("echo", new JsonObject { ["message"] = "héllo 中文" }, CancellationToken.None);
        Check(echo["message"]?.GetValue<string>() == "héllo 中文", "UTF-8 payload was changed.");
        Check(fixture.Operations().SequenceEqual(new[] { "plugin_handshake", "echo" }), "Startup issued an extra operation.");
        JsonObject invocation = JsonNode.Parse(await File.ReadAllTextAsync(fixture.File("invocation.json")))!.AsObject();
        Check(invocation["data_directory"]?.GetValue<string>() == fixture.Profile, "Wrong profile argument.");
        Check(invocation["synthetic"]?.GetValue<bool>() == true, "Synthetic mode was lost.");
        Check(invocation["working_directory"]?.GetValue<string>() == fixture.Package, "Working directory was inferred from the host.");
    }

    private static async Task BadHandshakes()
    {
        foreach (string mode in new[] { "bad_protocol", "bad_version", "bad_nonce", "bad_mode", "bad_path", "bad_synthetic", "bad_ruleset" })
        {
            using var fixture = new Fixture(mode);
            await ExpectFailure(() => fixture.Start());
            await fixture.AssertStopped();
            Check(fixture.Operations().SequenceEqual(new[] { "plugin_handshake" }), "A rejected handshake allowed business work.");
            fixture.SetMode("valid");
            await using var reopened = await fixture.Start();
        }
    }

    private static async Task NoSyntheticFlag()
    {
        using var fixture = new Fixture();
        // This remains the offline executable fixture; no live adapter is used.
        await using var client = await PluginWorkerClient.StartAsync(fixture.Package, fixture.Profile, false, CancellationToken.None);
        JsonObject invocation = JsonNode.Parse(await File.ReadAllTextAsync(fixture.File("invocation.json")))!.AsObject();
        Check(invocation["synthetic"]?.GetValue<bool>() == false, "Synthetic mode was inferred.");
    }

    private static async Task NewProfile()
    {
        using var fixture = new Fixture();
        string fresh = Path.Combine(fixture.Root, "new profile parent", "fresh profile");
        Check(!Directory.Exists(fresh), "The new profile fixture already existed.");
        await using var client = await PluginWorkerClient.StartAsync(fixture.Package, fresh, true, CancellationToken.None);
        Check(Directory.Exists(fresh), "The explicit new profile was unavailable to the worker.");
    }

    private static async Task OccupiedSingleton()
    {
        using var fixture = new Fixture();
        using var standalone = new Mutex(false, Singleton, out bool created);
        Check(created, "Another UPCAM backend is running; run acceptance on an isolated Windows user session.");
        await ExpectFailure(() => fixture.Start());
        Check(!File.Exists(fixture.File("worker.pid")), "Worker spawned despite standalone contention.");
    }

    private static async Task ShortLongAliases()
    {
        using var fixture = new Fixture();
        string longProfile = ExistingWindowsPath(fixture.Profile, shortForm: false);
        string shortProfile = ExistingWindowsPath(longProfile, shortForm: true);
        string longPackage = ExistingWindowsPath(fixture.Package, shortForm: false);
        string shortPackage = ExistingWindowsPath(longPackage, shortForm: true);
        Check(!string.Equals(shortProfile, longProfile, StringComparison.OrdinalIgnoreCase),
            "The Windows acceptance volume must provide an actual 8.3 profile alias for this regression.");
        Check(PluginWorkerClient.CanonicalizeDirectory(shortProfile, mustExist: true) == longProfile,
            "Existing short and long directory aliases had different identities.");
        Check(PluginWorkerClient.CanonicalizeDirectory(longProfile, mustExist: true) == longProfile,
            "The existing long directory identity changed.");
        string newShort = Path.Combine(shortProfile, "uncreated parent", "new explicit profile");
        string newLong = Path.Combine(longProfile, "uncreated parent", "new explicit profile");
        Check(!Directory.Exists(newLong), "New descendant profile already existed.");
        Check(PluginWorkerClient.CanonicalizeDirectory(newShort) == newLong,
            "An existing short ancestor was not expanded for a new profile.");
        await using (var client = await PluginWorkerClient.StartAsync(shortPackage, newShort, true, CancellationToken.None))
        {
            JsonObject invocation = JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(newLong, "invocation.json")))!.AsObject();
            Check(invocation["data_directory"]?.GetValue<string>() == newLong,
                "Worker did not receive the canonical long profile.");
            Check(invocation["working_directory"]?.GetValue<string>() == longPackage,
                "Worker did not receive the canonical long package directory.");
        }
        fixture.SetMode("bad_path");
        await ExpectFailure(() => PluginWorkerClient.StartAsync(shortPackage, shortProfile, true, CancellationToken.None));
        await fixture.AssertStopped();
        Check(fixture.Operations().SequenceEqual(new[] { "plugin_handshake" }),
            "Short alias canonicalization allowed a handshake bound to a different directory.");
    }

    private static async Task RacingStarts()
    {
        using var first = new Fixture();
        using var second = new Fixture();
        async Task<PluginWorkerClient?> TryStart(Fixture fixture)
        {
            try { return await fixture.Start(); }
            catch (InvalidOperationException) { return null; }
        }
        PluginWorkerClient?[] clients = await Task.WhenAll(TryStart(first), TryStart(second));
        try { Check(clients.Count(client => client is not null) == 1, "Concurrent sessions were not excluded."); }
        finally { foreach (var client in clients) if (client is not null) await client.DisposeAsync(); }
    }

    private static async Task UnsafePaths()
    {
        using var fixture = new Fixture();
        foreach (string path in new[] { "relative", @"C:relative", @"\\server\share\profile", Path.GetPathRoot(fixture.Profile)!,
            Path.Combine(fixture.Root, ".", "alias"), Path.Combine(fixture.Root, "..", "escape"), fixture.Profile + ":stream",
            Path.Combine(fixture.Root, "NUL"), fixture.Profile + ".", fixture.Profile + " " })
        {
            await ExpectFailure(() => PluginWorkerClient.StartAsync(path, fixture.Profile, true, CancellationToken.None));
            await ExpectFailure(() => PluginWorkerClient.StartAsync(fixture.Package, path, true, CancellationToken.None));
        }
        Check(!File.Exists(fixture.File("worker.pid")), "An invalid path launched a process.");
    }

    private static async Task ReparsePaths()
    {
        using var fixture = new Fixture();
        string profileLink = Path.Combine(fixture.Root, "profile junction");
        string packageLink = Path.Combine(fixture.Root, "package junction");
        CreateJunction(profileLink, fixture.Profile);
        CreateJunction(packageLink, fixture.Package);
        await ExpectFailure(() => PluginWorkerClient.StartAsync(fixture.Package, profileLink, true, CancellationToken.None));
        await ExpectFailure(() => PluginWorkerClient.StartAsync(packageLink, fixture.Profile, true, CancellationToken.None));
        string escapedPackage = Path.Combine(fixture.Root, "worker escape package");
        Directory.CreateDirectory(escapedPackage);
        CreateJunction(Path.Combine(escapedPackage, "worker"), Path.Combine(fixture.Package, "worker"));
        await ExpectFailure(() => PluginWorkerClient.StartAsync(escapedPackage, fixture.Profile, true, CancellationToken.None));
        Check(!File.Exists(fixture.File("worker.pid")), "A reparse path launched a process.");
    }

    private static async Task SafeErrors()
    {
        using var fixture = new Fixture();
        await using var client = await fixture.Start();
        Exception error = await ExpectFailure(() => client.SendAsync("backend_error", null, CancellationToken.None));
        Check(!error.ToString().Contains(PrivateMarker), "Backend error text escaped.");
        Check(client.IsUsable, "A business rejection broke the transport.");
        Exception known = await ExpectFailure(() => client.SendAsync("known_error", null, CancellationToken.None));
        Check(known.Message == "请先填写 Tenant ID 和 Project ID。", "An actionable safe setup message was lost.");
        foreach (var (operation, expected) in new[]
        {
            ("invalid_authorization_error", "自动运行授权请求格式无效；未采集或发送。"),
            ("changed_authorization_error", "自动运行授权已改变；请重新保存设置并明确启动。"),
            ("mutated_authorization_error", "自动运行期间不能修改已核验的设置或凭据；未保存任何更改。")
        })
        {
            Exception rejected = await ExpectFailure(() => client.SendAsync(operation, null, CancellationToken.None));
            Check(rejected.Message == expected && client.IsUsable, "An actionable safe authorization message was lost.");
        }
        Exception prefixed = await ExpectFailure(() => client.SendAsync("prefixed_error", null, CancellationToken.None));
        Check(!prefixed.ToString().Contains(PrivateMarker) && prefixed.Message != known.Message,
            "An unrecognized error was accepted by a partial allowlist match.");
        await client.SendAsync("echo", null, CancellationToken.None);
    }

    private static async Task BadResponses()
    {
        foreach (string mode in new[] { "invalid_utf8", "invalid_json", "wrong_id", "duplicate_id", "wrong_ok", "wrong_result", "extra_line", "partial_eof" })
        {
            using var fixture = new Fixture(mode);
            await using var client = await fixture.Start();
            Exception error = await ExpectFailure(() => client.SendAsync("probe", null, CancellationToken.None));
            Check(!error.ToString().Contains(PrivateMarker), "Protocol text escaped through diagnostics.");
            Check(!client.IsUsable, "A corrupt response left the connection usable.");
            await fixture.AssertStopped();
            await ExpectFailure(() => client.SendAsync("echo", null, CancellationToken.None));
            Check(fixture.Operations().Count(operation => operation == "probe") == 1, "A failed request was replayed.");
        }
    }

    private static async Task OversizedResponse()
    {
        using var fixture = new Fixture("oversized_response");
        await using var client = await fixture.Start();
        await ExpectFailure(() => client.SendAsync("probe", null, CancellationToken.None));
        Check(!client.IsUsable, "An oversized response left the connection usable.");
        await fixture.AssertStopped();
    }

    private static async Task OversizedRequest()
    {
        using var fixture = new Fixture();
        await using var client = await fixture.Start();
        await ExpectFailure(() => client.SendAsync("echo", new JsonObject { ["large"] = new string('x', 2 * 1024 * 1024) }, CancellationToken.None));
        Check(fixture.Operations().SequenceEqual(new[] { "plugin_handshake" }), "Oversized bytes reached the worker.");
        await client.SendAsync("echo", new JsonObject { ["after"] = true }, CancellationToken.None);
    }

    private static async Task SerializedCalls()
    {
        using var fixture = new Fixture();
        await using var client = await fixture.Start();
        JsonObject[] results = await Task.WhenAll(Enumerable.Range(0, 24).Select(index =>
            client.SendAsync("echo", new JsonObject { ["index"] = index }, CancellationToken.None)));
        Check(results.Select(result => result["index"]!.GetValue<int>()).SequenceEqual(Enumerable.Range(0, 24)), "Responses crossed callers.");
        Check(fixture.Operations().Count(operation => operation == "echo") == 24, "A request was lost or repeated.");
    }

    private static async Task DiscardStderr()
    {
        using var fixture = new Fixture("stderr_flood");
        await using var client = await fixture.Start();
        await client.SendAsync("probe", null, CancellationToken.None);
        Check(client.IsUsable, "Draining stderr broke the session.");
    }

    private static async Task CancelStartup()
    {
        using var fixture = new Fixture("hang_handshake");
        using var cancellation = new CancellationTokenSource();
        Task<PluginWorkerClient> pending = fixture.Start(cancellation.Token);
        await fixture.WaitFor("worker.pid");
        cancellation.Cancel();
        Check(await ExpectFailure(() => pending) is OperationCanceledException, "Startup cancellation lost its type.");
        await fixture.AssertStopped();
        fixture.SetMode("valid");
        await using var reopened = await fixture.Start();
    }

    private static async Task CancelLifetime()
    {
        using var fixture = new Fixture();
        using var lifetime = new CancellationTokenSource();
        await using var client = await fixture.Start(lifetime.Token);
        lifetime.Cancel();
        await fixture.AssertStopped();
        Check(!client.IsUsable, "Cancelled lifetime retained a usable worker.");
        await client.DisposeAsync();
        await using var reopened = await fixture.Start();
    }

    private static async Task CancelSend()
    {
        using var fixture = new Fixture("send_uncertain");
        await using (var client = await fixture.Start())
        {
            using var cancellation = new CancellationTokenSource();
            Task<JsonObject> pending = client.SendAsync("run_manual", null, cancellation.Token);
            await fixture.WaitFor("delivery.json");
            cancellation.Cancel();
            Check(await ExpectFailure(() => pending) is OperationCanceledException, "Request cancellation lost its type.");
            await fixture.AssertStopped();
            Check(!client.IsUsable, "Cancelled send retained a usable connection.");
        }
        fixture.SetMode("valid");
        await using var reopened = await fixture.Start();
        JsonObject recovered = await reopened.SendAsync("initialize", null, CancellationToken.None);
        Check(recovered["delivery_status"]?.GetValue<string>() == "unknown", "Unknown delivery was silently retried.");
        Check(fixture.Operations().Count(operation => operation == "run_manual") == 1, "A cancelled send was replayed.");
    }

    private static async Task CancelBeforeRequest()
    {
        using var fixture = new Fixture();
        await using var client = await fixture.Start();
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        Check(await ExpectFailure(() => client.SendAsync("echo", null, cancellation.Token)) is OperationCanceledException,
            "Pre-cancelled request lost its cancellation type.");
        await fixture.AssertStopped();
        Check(!client.IsUsable && fixture.Operations().SequenceEqual(new[] { "plugin_handshake" }),
            "Pre-cancelled request reached the worker or retained the connection.");
    }

    private static async Task RequestDeadline()
    {
        using var fixture = new Fixture("hang_request");
        await using var client = await fixture.Start();
        var elapsed = Stopwatch.StartNew();
        Check(await ExpectFailure(() => client.SendAsync("probe", null, CancellationToken.None)) is TimeoutException,
            "Ordinary request timeout lost its type.");
        Check(elapsed.Elapsed >= TimeSpan.FromSeconds(29) && elapsed.Elapsed < TimeSpan.FromSeconds(45), "Ordinary deadline was not enforced.");
        await fixture.AssertStopped();
        Check(fixture.Operations().Count(operation => operation == "probe") == 1, "Timed-out request was replayed.");
    }

    private static async Task CollectionDeadline()
    {
        using var fixture = new Fixture("long_collection");
        await using var client = await fixture.Start();
        foreach (string operation in new[] { "diagnose", "save", "plugin_run_due_authorized" })
        {
            JsonObject? payload = operation == "plugin_run_due_authorized"
                ? new JsonObject { ["authorization_fingerprint"] = new string('a', 64) } : null;
            await client.SendAsync(operation, payload, CancellationToken.None);
            Check(client.IsUsable, "A collection-capable operation used the ordinary deadline: " + operation);
        }
    }

    private static async Task DisposeBusy()
    {
        using var fixture = new Fixture("hang_request");
        var client = await fixture.Start();
        Task<JsonObject> pending = client.SendAsync("probe", null, CancellationToken.None);
        await fixture.WaitFor("request-active");
        await client.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        await ExpectFailure(() => pending);
        await fixture.AssertStopped();
        Check(fixture.Operations().Count(operation => operation == "probe") == 1, "Dispose replayed the request.");
    }

    private static async Task Shutdown()
    {
        using var fixture = new Fixture();
        using var lifetime = new CancellationTokenSource();
        var client = await fixture.Start(lifetime.Token);
        await client.SendAsync("spawn_descendant", null, CancellationToken.None);
        await client.DisposeAsync();
        await client.DisposeAsync();
        await fixture.AssertStopped();
        Check(fixture.Operations().Last() == "shutdown", "Graceful shutdown was skipped.");
        Check(IsStopped(int.Parse(await File.ReadAllTextAsync(fixture.File("descendant.pid")))), "Shutdown retained a descendant.");
        lifetime.Cancel();
        await using var reopened = await fixture.Start();
    }

    private static async Task HostDeath()
    {
        using var fixture = new Fixture();
        using var host = StartSelf("--crash-host", fixture.Package, fixture.Profile);
        try
        {
            await fixture.WaitFor("host-ready");
            int worker = int.Parse(await File.ReadAllTextAsync(fixture.File("worker.pid")));
            int descendant = int.Parse(await File.ReadAllTextAsync(fixture.File("descendant.pid")));
            host.Kill(entireProcessTree: false);
            await host.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
            await WaitUntil(() => IsStopped(worker) && IsStopped(descendant), "Host death retained a worker or descendant.");
        }
        finally
        {
            if (!host.HasExited)
            {
                host.Kill(entireProcessTree: true);
                await host.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
            }
        }
        await using var reopened = await fixture.Start();
    }

    private static async Task ShutdownDeadline()
    {
        using var fixture = new Fixture("hang_shutdown");
        var client = await fixture.Start();
        var elapsed = Stopwatch.StartNew();
        await client.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Check(elapsed.Elapsed < TimeSpan.FromSeconds(10), "Unresponsive shutdown was not bounded.");
        await fixture.AssertStopped();
        fixture.SetMode("valid");
        await using var reopened = await fixture.Start();
    }

    private static async Task<int> RunFixtureAsync(string[] args)
    {
        int profileIndex = Array.IndexOf(args, "--data-directory");
        string profile = args[profileIndex + 1];
        Directory.CreateDirectory(profile);
        string mode = File.Exists(Path.Combine(profile, "fixture.json"))
            ? JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(profile, "fixture.json")))!["mode"]!.GetValue<string>() : "valid";
        AtomicMarker(Path.Combine(profile, "worker.pid"), Environment.ProcessId.ToString());
        await File.WriteAllTextAsync(Path.Combine(profile, "invocation.json"), new JsonObject
        { ["data_directory"] = profile, ["synthetic"] = args.Contains("--synthetic"),
            ["working_directory"] = Environment.CurrentDirectory }.ToJsonString());
        using var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true));
        using Stream output = Console.OpenStandardOutput();
        while (await reader.ReadLineAsync() is { } line)
        {
            JsonObject request = JsonNode.Parse(line)!.AsObject();
            string operation = request["op"]!.GetValue<string>();
            string id = request["id"]!.GetValue<string>();
            await File.AppendAllTextAsync(Path.Combine(profile, "operations.txt"), operation + "\n");
            if (operation == "plugin_handshake")
            {
                if (mode == "hang_handshake") { await Task.Delay(Timeout.Infinite); return 0; }
                JsonObject result = request["payload"]!.DeepClone().AsObject();
                string nonce = result["nonce"]!.GetValue<string>();
                Check(result.Count == 3 && result["protocol_version"]!.GetValue<int>() == 1 &&
                    result["backend_version"]!.GetValue<string>() == "0.4.3" && nonce.Length == 32 &&
                    nonce.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f'),
                    "Client emitted an invalid handshake request.");
                result["mode"] = "project-workbench";
                result["data_directory"] = profile;
                result["synthetic"] = args.Contains("--synthetic");
                result["ruleset_revision"] = "fixture-rules-v1";
                switch (mode)
                {
                    case "bad_protocol": result["protocol_version"] = 2; break;
                    case "bad_version": result["backend_version"] = "0.4.2"; break;
                    case "bad_nonce": result["nonce"] = new string('0', 32); break;
                    case "bad_mode": result["mode"] = "standalone"; break;
                    case "bad_path": result["data_directory"] = Path.GetDirectoryName(profile); break;
                    case "bad_synthetic": result["synthetic"] = !args.Contains("--synthetic"); break;
                    case "bad_ruleset": result["ruleset_revision"] = ""; break;
                }
                await WriteResponse(output, id, result);
                continue;
            }
            if (operation == "shutdown")
            {
                if (mode == "hang_shutdown") { await Task.Delay(Timeout.Infinite); return 0; }
                await Task.Delay(150);
                await WriteResponse(output, id, new JsonObject { ["stopped"] = true });
                return 0;
            }
            if (operation == "spawn_descendant")
            {
                using var child = StartSelf("--descendant", Path.Combine(profile, "descendant.pid"));
                await WaitUntil(() => File.Exists(Path.Combine(profile, "descendant.pid")), "Fixture descendant did not start.");
                await WriteResponse(output, id, new JsonObject());
                continue;
            }
            if (operation is "backend_error" or "known_error" or "prefixed_error" or
                "invalid_authorization_error" or "changed_authorization_error" or "mutated_authorization_error")
            {
                string message = operation switch
                {
                    "known_error" => "请先填写 Tenant ID 和 Project ID。",
                    "prefixed_error" => "请先填写 Tenant ID 和 Project ID。" + PrivateMarker,
                    "invalid_authorization_error" => "自动运行授权请求格式无效；未采集或发送。",
                    "changed_authorization_error" => "自动运行授权已改变；请重新保存设置并明确启动。",
                    "mutated_authorization_error" => "自动运行期间不能修改已核验的设置或凭据；未保存任何更改。",
                    _ => PrivateMarker
                };
                await output.WriteAsync(Encoding.UTF8.GetBytes(new JsonObject
                { ["id"] = id, ["ok"] = false, ["error"] = message }.ToJsonString() + "\n"));
                await output.FlushAsync();
                continue;
            }
            if (operation == "initialize" && File.Exists(Path.Combine(profile, "delivery.json")))
            {
                await WriteResponse(output, id, new JsonObject { ["delivery_status"] = "unknown" });
                continue;
            }
            if (mode == "send_uncertain")
            {
                await File.WriteAllTextAsync(Path.Combine(profile, "delivery.json"), "{\"status\":\"sending\"}");
                await Task.Delay(Timeout.Infinite);
                return 0;
            }
            if (mode == "hang_request")
            {
                await File.WriteAllTextAsync(Path.Combine(profile, "request-active"), "active");
                await Task.Delay(Timeout.Infinite);
                return 0;
            }
            switch (mode)
            {
                case "invalid_utf8": await output.WriteAsync(new byte[] { 0xc3, 0x28, 0x0a }); break;
                case "invalid_json": await output.WriteAsync(Encoding.UTF8.GetBytes(PrivateMarker + "\n")); break;
                case "wrong_id": await WriteResponse(output, "different-id", new JsonObject()); break;
                case "duplicate_id": await output.WriteAsync(Encoding.UTF8.GetBytes($"{{\"id\":\"{id}\",\"id\":\"{id}\",\"ok\":true,\"result\":{{}}}}\n")); break;
                case "wrong_ok": await output.WriteAsync(Encoding.UTF8.GetBytes($"{{\"id\":\"{id}\",\"ok\":\"true\",\"result\":{{}}}}\n")); break;
                case "wrong_result": await output.WriteAsync(Encoding.UTF8.GetBytes($"{{\"id\":\"{id}\",\"ok\":true,\"result\":[]}}\n")); break;
                case "extra_line":
                    string response = new JsonObject { ["id"] = id, ["ok"] = true, ["result"] = new JsonObject() }.ToJsonString() + "\n";
                    await output.WriteAsync(Encoding.UTF8.GetBytes(response + response)); break;
                case "partial_eof": await output.WriteAsync(Encoding.UTF8.GetBytes("{")); await output.FlushAsync(); return 0;
                case "oversized_response":
                    byte[] chunk = Enumerable.Repeat((byte)'x', 8192).ToArray();
                    for (int count = 0; count < 4097; count++) await output.WriteAsync(chunk);
                    await output.WriteAsync(new byte[] { 0x0a }); break;
                case "stderr_flood":
                    using (Stream stderr = Console.OpenStandardError())
                    {
                        byte[] block = Enumerable.Repeat((byte)'s', 8192).ToArray();
                        for (int count = 0; count < 4096; count++) await stderr.WriteAsync(block);
                    }
                    await WriteResponse(output, id, new JsonObject()); break;
                case "long_collection":
                    if (operation == "plugin_run_due_authorized")
                    {
                        JsonObject authorization = request["payload"]!.AsObject();
                        Check(authorization.Count == 1 && authorization["authorization_fingerprint"]?.GetValue<string>() == new string('a', 64),
                            "Managed fixture requires its exact synthetic authorization fingerprint.");
                    }
                    await Task.Delay(TimeSpan.FromSeconds(31));
                    await WriteResponse(output, id, new JsonObject());
                    break;
                default: await WriteResponse(output, id, request["payload"]!.DeepClone().AsObject()); break;
            }
            await output.FlushAsync();
        }
        return 0;
    }

    private static async Task WriteResponse(Stream output, string id, JsonObject result)
    {
        await output.WriteAsync(Encoding.UTF8.GetBytes(new JsonObject
        { ["id"] = id, ["ok"] = true, ["result"] = result }.ToJsonString() + "\n"));
        await output.FlushAsync();
    }

    private static void AtomicMarker(string path, string value)
    {
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, value);
        File.Move(temporary, path, overwrite: true);
    }

    private static Process StartSelf(params string[] args)
    {
        var start = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true };
        foreach (string argument in args) start.ArgumentList.Add(argument);
        return Process.Start(start) ?? throw new InvalidOperationException("Fixture process did not start.");
    }

    private static async Task<Exception> ExpectFailure(Func<Task> action)
    {
        try { await action(); }
        catch (Exception error) { return error; }
        throw new InvalidOperationException("Expected the operation to fail.");
    }

    private static void Check(bool condition, string message)
    { if (!condition) throw new InvalidOperationException(message); }

    private static bool IsStopped(int processId)
    {
        try { using var process = Process.GetProcessById(processId); return process.HasExited; }
        catch (ArgumentException) { return true; }
    }

    private static async Task WaitUntil(Func<bool> condition, string failure)
    {
        var deadline = Stopwatch.StartNew();
        while (!condition())
        {
            if (deadline.Elapsed > TimeSpan.FromSeconds(10)) throw new TimeoutException(failure);
            await Task.Delay(20);
        }
    }

    private static void CreateJunction(string link, string target)
    {
        var start = new ProcessStartInfo("cmd.exe") { UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true };
        // cmd parses /c as shell text, so do not apply CommandLineToArgvW
        // escaping to its embedded quotes. Both paths are generated fixtures.
        start.Arguments = $"/d /c mklink /J \"{link}\" \"{target}\"";
        using var process = Process.Start(start)!;
        process.StandardOutput.ReadToEnd(); process.StandardError.ReadToEnd(); process.WaitForExit();
        Check(process.ExitCode == 0, "Could not create the offline junction fixture.");
    }

    private static string ExistingWindowsPath(string path, bool shortForm)
    {
        // Independent OS oracle for fixtures, not the production canonicalizer.
        var buffer = new StringBuilder(32768);
        uint count = shortForm ? GetShortPathNameW(path, buffer, (uint)buffer.Capacity) :
            GetLongPathNameW(path, buffer, (uint)buffer.Capacity);
        Check(count > 0 && count < buffer.Capacity, "Could not obtain the Windows fixture path form.");
        return Path.TrimEndingDirectorySeparator(buffer.ToString());
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint GetLongPathNameW(string path, StringBuilder buffer, uint length);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint GetShortPathNameW(string path, StringBuilder buffer, uint length);

    private sealed class Fixture : IDisposable
    {
        public string Root { get; } = Path.Combine(ExistingWindowsPath(Path.GetTempPath(), shortForm: false),
            "UPCAM transport spaces " + Guid.NewGuid().ToString("N"));
        public string Package => Path.Combine(Root, "plugin package");
        public string Profile => Path.Combine(Root, "explicit profile");
        public Fixture(string mode = "valid")
        {
            string worker = Path.Combine(Package, "worker");
            Directory.CreateDirectory(worker); Directory.CreateDirectory(Profile);
            // Apphost binds to its original assembly name even when the exe is renamed.
            foreach (string source in Directory.EnumerateFiles(AppContext.BaseDirectory))
            {
                string filename = Path.GetFileName(source);
                if (filename.StartsWith("UPCAM.Workbench.TransportTests", StringComparison.OrdinalIgnoreCase))
                    System.IO.File.Copy(source, Path.Combine(worker, filename));
            }
            System.IO.File.Copy(Environment.ProcessPath!, Path.Combine(worker, "UPCAMPluginWorker.exe"));
            SetMode(mode);
        }
        public string File(string name) => Path.Combine(Profile, name);
        public void SetMode(string mode) => System.IO.File.WriteAllText(File("fixture.json"), new JsonObject { ["mode"] = mode }.ToJsonString());
        public Task<PluginWorkerClient> Start(CancellationToken token = default) => PluginWorkerClient.StartAsync(Package, Profile, true, token);
        public string[] Operations() => System.IO.File.Exists(File("operations.txt")) ? System.IO.File.ReadAllLines(File("operations.txt")) : [];
        public Task WaitFor(string name) => WaitUntil(() => System.IO.File.Exists(File(name)), "Fixture marker did not appear: " + name);
        public async Task AssertStopped()
        {
            await WaitFor("worker.pid");
            int worker = int.Parse(await System.IO.File.ReadAllTextAsync(File("worker.pid")));
            await WaitUntil(() => IsStopped(worker), "Owned worker did not stop.");
        }
        public void Dispose()
        {
            // Remove junctions before recursive deletion; never follow a fixture link.
            foreach (string path in new[] { Path.Combine(Root, "profile junction"), Path.Combine(Root, "package junction"),
                Path.Combine(Root, "worker escape package", "worker") })
                if (Directory.Exists(path)) Directory.Delete(path);
            try { Directory.Delete(Root, recursive: true); }
            catch (IOException) { /* A failed test retains its disposable synthetic evidence. */ }
            catch (UnauthorizedAccessException) { }
        }
    }
}
