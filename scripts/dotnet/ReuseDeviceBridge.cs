using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.IO.Ports;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

// Bounded operations using vendor EntityModbus, CRC16, ByteUtil, send_msg and
// ConnectSocket. No application Main, UI or communication scheduler is started.
internal static class ReuseDeviceBridge
{
    private const string VendorSha = "05b9be5e2e2a2cb74037c32451f89b0117dfa80983e4ff11b7b7c67912553f47";
    private static Assembly vendor;
    private static Type commType, entityType, actionType;
    private static object comm;
    private static SerialPort serial;
    private static Socket socket;
    private static Mutex deviceLock;
    private static bool ownsLock, writeAttempted;
    private static Timer watchdog;
    private static StreamWriter runJournal;
    private static BlockingCollection<object> logQueue;
    private static Thread logThread;
    private static volatile bool logFailed, commandReaderStopped;
    private static BlockingCollection<QueuedCommand> commandQueue;
    private static BlockingCollection<QueuedCommand> controlQueue;
    private static bool processingControl;
    private static Thread commandReader;
    private static int pendingCommandResults;
    private sealed class QueuedCommand
    { public string Path, Error; public Dictionary<string, object> Input; }
    private static Action serviceRun;
    private static bool transportFailed, runFault, suppressCancellation;
    private static int responseDeadlineMs = 3000;
    private static Stopwatch responseClock = Stopwatch.StartNew();
    private static MotionSession motion;
    private sealed class MotionSession
    {
        public int Raw, MaximumRaw, Revision, InitialDirection;
        public ushort Idle;
        public double Setpoint, Maximum, Scale;
        public string Unit;
        public bool Position, AllowDirectionChange;
        public Dictionary<string, object> Limits;
    }
    private static readonly object logLock = new object();
    private static readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 8000000 };
    private static readonly List<object> frames = new List<object>();
    private static Type T(string n) { return vendor.GetType("BonengDrivesoft." + n, true); }
    private static object Call(Type t, object obj, string n, Type[] sig, params object[] values)
    { return t.GetMethod(n, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static, null, sig, null).Invoke(obj, values); }
    private static void Require(bool ok, string reason) { if (!ok) throw new InvalidOperationException(reason); }
    private static int Int(Dictionary<string, object> r, string k) { return Convert.ToInt32(r[k]); }
    private static string Str(Dictionary<string, object> r, string k) { return Convert.ToString(r[k]); }
    private static void Emit(object value)
    {
        if (logQueue != null) { logQueue.Add(value); return; }
        WriteLog(value, json);
    }
    private static void WriteLog(object value, JavaScriptSerializer serializer)
    {
        lock (logLock)
        {
            string line = serializer.Serialize(value);
            if (runJournal != null) { runJournal.WriteLine(line); runJournal.Flush(); }
            try { Console.WriteLine(line); Console.Out.Flush(); } catch (IOException) { }
        }
    }
    private static void StartRunServices(Dictionary<string, object> input)
    {
        logQueue = new BlockingCollection<object>();
        logThread = new Thread(delegate() {
            var serializer = new JavaScriptSerializer { MaxJsonLength = 8000000 };
            try { foreach (object row in logQueue.GetConsumingEnumerable()) WriteLog(row, serializer); }
            catch (Exception) { logFailed = true; }
        }); logThread.IsBackground = true; logThread.Start();
        if (!input.ContainsKey("command_dir")) return;
        string folder = Str(input, "command_dir");
        commandQueue = new BlockingCollection<QueuedCommand>(32);
        controlQueue = new BlockingCollection<QueuedCommand>(32);
        commandReader = new Thread(delegate() {
            var seen = new HashSet<string>(); var serializer = new JavaScriptSerializer { MaxJsonLength = 8000000 };
            while (!commandReaderStopped)
            {
                try
                {
                    string[] files = Directory.GetFiles(folder, "*.request.json"); Array.Sort(files, StringComparer.Ordinal);
                    foreach (string path in files)
                    {
                        if (seen.Contains(path)) continue;
                        var command = new QueuedCommand { Path = path };
                        try { Require(new FileInfo(path).Length <= 8000000, "Queued request exceeds size limit."); command.Input = serializer.Deserialize<Dictionary<string, object>>(File.ReadAllText(path, Encoding.UTF8)); }
                        catch (Exception e) { command.Error = e.Message; }
                        bool priority = command.Input != null && (Str(command.Input, "operation") == "update_speed" || Str(command.Input, "operation") == "motion_state");
                        if ((priority ? controlQueue : commandQueue).TryAdd(command)) seen.Add(path);
                    }
                    seen.RemoveWhere(delegate(string p) { return !File.Exists(p); });
                }
                catch (Exception) { }
                Thread.Sleep(10);
            }
        }); commandReader.IsBackground = true; commandReader.Start();
    }
    private static byte[] Exact(int count)
    {
        byte[] data = new byte[count]; int pos = 0;
        Stopwatch deadline = Stopwatch.StartNew();
        while (pos < count)
        {
            if (deadline.ElapsedMilliseconds >= 3000 || responseClock.ElapsedMilliseconds >= responseDeadlineMs)
            { transportFailed = true; throw new IOException("Response deadline exceeded; command is not retried."); }
            int available = socket == null ? serial.BytesToRead : socket.Available;
            if (available == 0) { Thread.Sleep(1); continue; }
            int n;
            try { n = socket == null ? serial.Read(data, pos, Math.Min(available, count - pos)) : socket.Receive(data, pos, Math.Min(available, count - pos), SocketFlags.None); }
            catch { transportFailed = true; throw; }
            Require(n > 0, "Device disconnected during response."); pos += n;
        }
        return data;
    }
    private static void Send(byte[] request, bool writes)
    {
        if (serviceRun != null) serviceRun();
        ushort crc = (ushort)Call(commType, null, "CRC16", new Type[] { typeof(byte[]), typeof(int), typeof(int) }, request, 0, request.Length - 2);
        request[request.Length - 2] = (byte)(crc >> 8); request[request.Length - 1] = (byte)crc;
        if (writes) writeAttempted = true;
        responseClock.Restart();
        if (!(bool)Call(commType, comm, "send_msg", new Type[] { typeof(byte[]) }, request))
        { transportFailed = true; throw new IOException("Vendor send failed; no automatic retry."); }
    }
    private static byte[] Exchange(byte[] request, int length, bool writes)
    {
        Send(request, writes); byte[] result = Exact(length); Check(request, result);
        frames.Add(new { request_hex = BitConverter.ToString(request), response_hex = BitConverter.ToString(result), writes = writes });
        if (frames.Count > 200) frames.RemoveAt(0);
        return result;
    }
    private static void Check(byte[] request, byte[] response)
    {
        if (!(bool)Call(commType, comm, "check_crc", new Type[] { typeof(byte[]) }, response) || response[0] != request[0] || response[1] != request[1] || response[2] != request[2])
        { transportFailed = true; throw new IOException("Vendor CRC validation or response identity failed."); }
    }
    private static byte[] EntityFrame(ushort sof, byte type, byte group, byte offset, byte count)
    {
        object e = entityType.GetConstructor(new Type[] { typeof(ushort), typeof(byte), typeof(byte), typeof(byte), typeof(byte), typeof(int), actionType, typeof(int) })
            .Invoke(new object[] { sof, type, group, offset, count, 6, null, 0 });
        return (byte[])entityType.GetField("read_inquire").GetValue(e);
    }
    private static byte[] ParameterFrame(ushort sof, byte type, byte group, byte offset, byte count, int bytes)
    {
        byte[] response = Exchange(EntityFrame(sof, type, group, offset, count), 8 + bytes * count, false);
        Require(response[3] == group && response[4] == offset && response[5] == count, "Parameter response address/count mismatch.");
        return response;
    }
    private static ushort Word(byte group, byte offset)
    { return BitConverter.ToUInt16(ParameterFrame(0x55CC, 0xFB, group, offset, 1, 3), 6); }
    private static Dictionary<string, object> ProtocolIdentity(bool tcp, bool inspectSerial = false)
    {
        byte[] version = Exchange(new byte[] { 0xAA, 0x55, 0xA5, 0xA5, 0, 0 }, 8, false);
        Require(version[3] == 0xA5, "Parameter-version handshake mismatch.");
        byte[] identity = Exchange(new byte[] { 0xAA, 0x55, 0xFA, 0, 0, 0 }, 10, false);
        Require(identity[3] == 2 && BitConverter.ToUInt16(identity, 6) == (tcp ? 3 : 0), "Platform/transport handshake mismatch.");
        // The current connection's MAC/software bootstrap is inspected for AN.
        // Metadata is discovered online; other platform identity layouts remain unsupported.
        int platform = BitConverter.ToUInt16(identity, 4);
        Require(platform == 9 || (inspectSerial && !tcp && platform == 3), "This identity adapter supports AN platform 9, or CM55 platform 3 for serial inspection only.");
        return new Dictionary<string, object> {
            { "parameter_version_raw", (int)BitConverter.ToUInt16(version, 4) },
            { "platform_type", (int)BitConverter.ToUInt16(identity, 4) },
            { "communication_type", (int)BitConverter.ToUInt16(identity, 6) } };
    }
    private static Dictionary<string, object> BoundIdentity(bool tcp, Dictionary<string, object> input)
    {
        bool cm55 = input.ContainsKey("device_profile") && Str(input, "device_profile") == "cm55_ec_612";
        Require(!cm55 || (!tcp && System.Text.RegularExpressions.Regex.IsMatch(Str(input, "serial_binding_id"), "^[0-9a-f]{32}$")), "CM55 requires a selected serial binding.");
        var identity = ProtocolIdentity(tcp, cm55);
        ushort software = Word(0x22, 0), release = Word(0x22, 8);
        if (cm55)
        {
            Require(Int(identity, "platform_type") == 3 && software == 612, "CM55 platform/software mismatch; no write sent.");
            identity["mac"] = null; identity["device_profile"] = "cm55_ec_612";
            identity["board_number_raw"] = (int)Word(0x22, 10); identity["platform_raw"] = (int)Word(0x22, 11);
            Require(Int(identity, "platform_raw") % 10000 == 301, "This CM55 profile requires the EC platform variant.");
            identity["serial_binding_id"] = Str(input, "serial_binding_id");
            if (input.ContainsKey("expected_serial_identity"))
            {
                var expected = (Dictionary<string, object>)input["expected_serial_identity"];
                foreach (string key in new string[] { "platform_type", "communication_type", "board_number_raw", "platform_raw" })
                    Require(expected.ContainsKey(key) && Convert.ToString(expected[key]) == Convert.ToString(identity[key]), "Selected serial device fingerprint changed; no write sent.");
            }
            else Require(Str(input, "operation") == "identity" && input.ContainsKey("bind_serial") && Convert.ToBoolean(input["bind_serial"]), "Select the CM55 serial endpoint before using device tools.");
        }
        else
        {
            ushort m1 = Word(0x45, 19), m2 = Word(0x45, 20), m3 = Word(0x45, 21);
            string actual = BitConverter.ToString(new byte[] { (byte)m1, (byte)(m1 >> 8), (byte)m2, (byte)(m2 >> 8), (byte)m3, (byte)(m3 >> 8) }).Replace('-', ':');
            Require(actual == Str(input, "expected_mac"), "Connected MAC differs from the selected target.");
            identity["mac"] = actual;
        }
        identity["software_raw"] = (int)software; identity["release_hex"] = release.ToString("X4");
        return identity;
    }
    private static string DeviceText(byte[] data, int start, int length)
    { return Encoding.GetEncoding(936).GetString(data, start, length).TrimEnd('\0', ' '); }
    private static Dictionary<string, object> Attribute(uint attr)
    {
        int unit = (int)((attr >> 4) & 63);
        string[] units = (string[])T("util.ValueUtil").GetField("Unit").GetValue(null);
        return new Dictionary<string, object> {
            { "attribute_raw", attr }, { "width_bits", (attr & (1u << 29)) != 0 ? 32 : 16 },
            { "signed", (attr & (1u << 18)) != 0 }, { "access", (int)((attr >> 12) & 3) },
            { "unit_index", unit }, { "unit", unit < units.Length ? units[unit] : null },
            { "limit_is_reference", (attr & (1u << 21)) != 0 }, { "reboot_required", (attr & (1u << 28)) != 0 } };
    }
    private static object DiscoverCatalog(Dictionary<string, object> input)
    {
        Stopwatch deadline = Stopwatch.StartNew();
        byte[] six = Exchange(new byte[] { 0xBB, 0x55, 0xFF, 0, 0, 0 }, 6, false);
        int majorCount = six[3], sequence = 0;
        Require(majorCount > 0 && majorCount <= 8, "Unsupported or invalid B6 major-group count.");
        string filter = input.ContainsKey("group_filter") ? Str(input, "group_filter") : "";
        bool matched = filter == "";
        var majors = new List<object>(); var groups = new List<object>(); var parameters = new List<object>();
        var addresses = new HashSet<int>(); var codes = new HashSet<string>();
        for (int major = 0; major < majorCount; major++)
        {
            byte[] request = new byte[] { 0xBB, 0x55, 0xFD, (byte)major, 0, 0 };
            Send(request, false); byte[] head = Exact(6);
            Require(head[3] == major && head[4] <= 16 && head[5] > 0 && head[5] <= 128, "Invalid B4 address/count/string length.");
            byte[] response = new byte[8 + head[5]]; Array.Copy(head, response, 6); Array.Copy(Exact(head[5] + 2), 0, response, 6, head[5] + 2); Check(request, response);
            string majorName = DeviceText(response, 6, head[5]);
            Require(majorName.Length > 0 && "ABCDEFGP".IndexOf(majorName[0]) >= 0, "B4 has no recognized function-code group prefix.");
            string letter = majorName.Substring(0, 1);
            majors.Add(new { index = major, name = majorName, subgroup_count = (int)head[4] });
            for (int child = 0; child < head[4]; child++, sequence++)
            {
                Require(sequence < 128 && deadline.ElapsedMilliseconds < 50000, "Catalog discovery exceeded its bounded size/time; retry one group at a time.");
                byte[] fiveRequest = new byte[] { 0xBB, 0x55, 0xFE, (byte)sequence, 0, 0 };
                Send(fiveRequest, false); byte[] five = Exact(7);
                Require(five[3] == sequence && five[4] <= 100 && five[6] > 0 && five[6] <= 128, "Invalid B5 address/count/string length.");
                byte[] groupResponse = new byte[9 + five[6]]; Array.Copy(five, groupResponse, 7); Array.Copy(Exact(five[6] + 2), 0, groupResponse, 7, five[6] + 2); Check(fiveRequest, groupResponse);
                byte wireGroup = five[5]; int count = five[4];
                string codeGroup = letter + (wireGroup & 15).ToString("D2"), groupName = DeviceText(groupResponse, 7, five[6]);
                Require(addresses.Add(wireGroup) && codes.Add(codeGroup), "Duplicate B5 wire address or function-code group.");
                groups.Add(new { index = sequence, major_index = major, code = codeGroup, group = (int)wireGroup, name = groupName, parameter_words = count });
                if (filter != "" && filter != codeGroup) continue;
                matched = true;
                var attrs = new uint[count]; var names = new string[count];
                for (int offset = 0; offset < count; offset += 30)
                {
                    int n = Math.Min(30, count - offset);
                    byte[] a = ParameterFrame(0x55BB, 0xFA, wireGroup, (byte)offset, (byte)n, 4);
                    for (int i = 0; i < n; i++) attrs[offset + i] = BitConverter.ToUInt32(a, 6 + 4 * i);
                }
                for (int offset = 0; offset < count; offset += 3)
                {
                    Require(deadline.ElapsedMilliseconds < 50000, "Catalog discovery exceeded 50 seconds; retry one group at a time.");
                    int n = Math.Min(3, count - offset);
                    byte[] a = ParameterFrame(0x55BB, 0xFC, wireGroup, (byte)offset, (byte)n, 32);
                    for (int i = 0; i < n; i++)
                    {
                        byte[] name = new byte[32]; Array.Copy(a, 6 + 32 * i, name, 0, 32);
                        names[offset + i] = ((string)Call(T("util.ByteUtil"), null, "ExChangeString", new Type[] { typeof(byte[]) }, name)).TrimEnd('\0', ' ');
                    }
                }
                string highOf = null;
                for (int offset = 0; offset < count; offset++)
                {
                    string code = codeGroup + "." + offset.ToString("D2");
                    var p = Attribute(attrs[offset]); p["code"] = code; p["group"] = (int)wireGroup; p["offset"] = offset;
                    p["group_name"] = groupName; p["name"] = names[offset]; p["high_word_of"] = highOf;
                    p["reserved"] = String.IsNullOrWhiteSpace(names[offset]) || names[offset].Contains("预留") || names[offset].Contains("保留");
                    p["origin"] = "live_B1_B3"; parameters.Add(p);
                    if (highOf != null) highOf = null;
                    else if (Int(p, "width_bits") == 32)
                    { Require(offset + 1 < count, "32-bit parameter extends beyond B5 parameter-word count."); highOf = code; }
                }
            }
        }
        Require(matched, "Requested function-code group was not advertised by B4/B5.");
        return new { major_groups = majors, groups = groups, parameters = parameters, group_filter = filter, complete = filter == "", definitions_complete_for_scope = true,
            metadata_commands = new string[] { "B6", "B4", "B5", "B1", "B3" }, enumeration_text_available = false, elapsed_ms = deadline.ElapsedMilliseconds };
    }
    private static Dictionary<string, object> Definition(byte group, byte offset, bool anControlLayout = true)
    {
        uint attr = BitConverter.ToUInt32(ParameterFrame(0x55BB, 0xFA, group, offset, 1, 4), 6);
        byte[] limits = ParameterFrame(0x55BB, 0xFB, group, offset, 1, 12);
        byte[] nameFrame = ParameterFrame(0x55BB, 0xFC, group, offset, 1, 32);
        byte[] nameBytes = new byte[32]; Array.Copy(nameFrame, 6, nameBytes, 0, 32);
        string name = (string)Call(T("util.ByteUtil"), null, "ExChangeString", new Type[] { typeof(byte[]) }, nameBytes);
        bool wide = (attr & (1u << 29)) != 0;
        Require(!wide || offset < 99, "32-bit parameter crosses the group boundary.");
        byte[] values = ParameterFrame(0x55CC, 0xFB, group, offset, (byte)(wide ? 2 : 1), 3);
        uint raw = BitConverter.ToUInt16(values, 6);
        if (wide) raw |= (uint)BitConverter.ToUInt16(values, 9) << 16;
        // P12.07's attribute byte is overloaded by the firmware with the
        // control-owner ID (AN TCP=253, AN/CM55 USB=254), not decimal places.
        bool ownerAttribute = anControlLayout && group == 0xEC && offset == 7;
        int point = ownerAttribute ? 0 : values[8]; bool signed = (attr & (1u << 18)) != 0;
        Require(!anControlLayout || point <= 4, "Unexpected decimal attribute; raw response retained.");
        long decoded = signed ? (wide ? (long)unchecked((int)raw) : (long)unchecked((short)raw)) : (long)raw;
        int access = (int)((attr >> 12) & 3);
        string[] units = (string[])T("util.ValueUtil").GetField("Unit").GetValue(null);
        return new Dictionary<string, object> {
            {"group", (int)group}, {"offset", (int)offset}, {"name", name.TrimEnd('\0', ' ')}, {"attribute_raw", attr},
            {"lower_raw", BitConverter.ToUInt32(limits, 6)}, {"upper_raw", BitConverter.ToUInt32(limits, 10)},
            {"factory_raw", BitConverter.ToUInt32(limits, 14)}, {"raw_value", raw}, {"width_bits", wide ? 32 : 16},
            {"signed", signed}, {"decimal_attribute_raw", point}, {"access", access},
            {"control_owner", ownerAttribute ? (object)values[8] : null},
            {"unit_index", (int)((attr >> 4) & 63)}, {"unit", ((attr >> 4) & 63) < units.Length ? units[(attr >> 4) & 63] : null}, {"value", access == 3 || point > 4 ? null : (object)(decoded / Math.Pow(10, point))},
            {"engineering_value_available", access != 3 && point <= 4},
            {"readback_meaningful", access != 3}, {"limit_is_reference", (attr & (1u << 21)) != 0},
            {"reboot_required", (attr & (1u << 28)) != 0} };
    }
    private static void Write(byte group, byte offset, uint raw, bool wide)
    {
        byte[] request = new byte[wide ? 12 : 10];
        request[0] = 0xCC; request[1] = 0x55; request[2] = wide ? (byte)0xFF : (byte)0xFC;
        request[3] = group; request[4] = offset; request[5] = 1;
        byte[] value = BitConverter.GetBytes(raw); Array.Copy(value, 0, request, 6, wide ? 4 : 2);
        byte[] result = Exchange(request, 9, true);
        Require(result[3] == group && result[4] == offset && result[5] == 1, "Write response address/count mismatch; do not retry.");
        Require(result[6] == 0, "Device rejected write with status " + result[6] + " (1 range, 2 read-only, 3 authority, 4 stopped-only).");
    }
    private static byte[] ScopePoll(int channels)
    {
        byte[] request = new byte[] { 0xEE, 0x55, 0xFB, 1, 0, 0 }; Send(request, false);
        byte[] header = Exact(6); Require(header[4] == 1 && header[5] <= 40, "Invalid scope response status/count.");
        byte[] tail = Exact(2 + 2 * header[5] * channels), response = new byte[header.Length + tail.Length];
        Array.Copy(header, response, 6); Array.Copy(tail, 0, response, 6, tail.Length); Check(request, response);
        return response;
    }
    private static object SnapshotValues(object[] addresses)
    {
        var values = new List<object>(); Stopwatch deadline = Stopwatch.StartNew();
        foreach (Dictionary<string, object> a in addresses)
        {
            Require(deadline.ElapsedMilliseconds < 50000, "Snapshot exceeded 50 seconds; retry a smaller group selection.");
            byte g = checked((byte)Int(a, "group")), o = checked((byte)Int(a, "offset"));
            Require(g >= 0x20 && g <= 0xEF && o < 100, "Invalid snapshot address.");
            uint attr = BitConverter.ToUInt32(ParameterFrame(0x55BB, 0xFA, g, o, 1, 4), 6);
            Require(attr == Convert.ToUInt32(a["expected_attribute"]), "Snapshot definition changed; refresh the online catalog.");
            var p = Attribute(attr); Require(Int(p, "access") != 3, "Write-only parameter cannot be backed up.");
            bool wide = Int(p, "width_bits") == 32; Require(!wide || o < 99, "Invalid 32-bit snapshot address.");
            byte[] data = ParameterFrame(0x55CC, 0xFB, g, o, (byte)(wide ? 2 : 1), 3);
            uint raw = BitConverter.ToUInt16(data, 6); if (wide) raw |= (uint)BitConverter.ToUInt16(data, 9) << 16;
            int point = g == 0xEC && o == 7 ? 0 : data[8]; Require(point <= 4, "Unexpected snapshot decimal attribute.");
            p["code"] = Str(a, "code"); p["group"] = (int)g; p["offset"] = (int)o;
            p["raw_value"] = raw; p["decimal_attribute_raw"] = point; p["read_elapsed_ms"] = deadline.ElapsedMilliseconds;
            values.Add(p);
        }
        return new { parameters = values, elapsed_ms = deadline.ElapsedMilliseconds, simultaneous_snapshot = false };
    }
    private static void CheckExpectedIdentity(Dictionary<string, object> input, Dictionary<string, object> identity)
    {
        if (!input.ContainsKey("expected_identity")) return;
        var expected = (Dictionary<string, object>)input["expected_identity"];
        foreach (string key in identity.Keys) Require(expected.ContainsKey(key) && Convert.ToString(expected[key]) == Convert.ToString(identity[key]), "Device/parameter identity changed since catalog discovery or preview; no write sent.");
    }
    private static double Number(Dictionary<string, object> r, string key)
    { double n = Convert.ToDouble(r[key]); Require(!Double.IsNaN(n) && !Double.IsInfinity(n), "Non-finite " + key); return n; }
    private static Dictionary<string, object> NamedDefinition(byte group, byte offset, string chinese, string english, int width)
    {
        var p = Definition(group, offset); string name = Str(p, "name");
        Require(Int(p, "width_bits") == width && (name.Contains(chinese) || name.IndexOf(english, StringComparison.OrdinalIgnoreCase) >= 0),
            "Live motion parameter definition does not match this inspected product adapter: " + name);
        return p;
    }
    private static Dictionary<string, object> MotorFingerprint()
    {
        int selected = (Word(0xEC, 8) >> 9) & 3; byte g = (byte)(0x50 + selected * 3);
        int type = Word(g, 0), frequency = Word(g, 4), rated = Word(g, 5), maximumPercent = Word(g, 6), minimumPercent = Word(g, 7), poles = Word(g, 9);
        int basis = type == 1 || poles == 0 ? rated : frequency * 6 / (poles * 10);
        Require(selected == ((Word(0xEC, 8) >> 9) & 3), "Active motor data group changed during reading.");
        double maximum = basis * Math.Max(maximumPercent, minimumPercent) / 1000.0;
        Require(rated > 0 && maximum > 0 && basis > 0 && basis <= 32767 && frequency > 0, "Invalid motor rating/speed base; no motion command sent.");
        return new Dictionary<string, object> { { "motor_data_group", selected }, { "motor_type_raw", type }, { "rated_frequency_raw", frequency },
            { "rated_rpm", rated }, { "motor_max_rpm", maximum }, { "motor_max_percent_raw", maximumPercent }, { "motor_min_percent_raw", minimumPercent },
            { "pole_pairs", poles }, { "base_rpm", basis } };
    }
    private static void EqualFields(Dictionary<string, object> actual, Dictionary<string, object> expected, string error)
    { foreach (string key in actual.Keys) Require(expected.ContainsKey(key) && Convert.ToString(actual[key]) == Convert.ToString(expected[key]), error + ": " + key); }
    private static Dictionary<string, object> MotionLimits()
    {
        var limits = MotorFingerprint(); byte group = (byte)(0x50 + Int(limits, "motor_data_group") * 3);
        var rated = NamedDefinition(group, 5, "额定转速", "rated speed", 16);
        var maximum = NamedDefinition(group, 6, "最大转速", "maximum speed", 16);
        var frequency = NamedDefinition(group, 4, "额定频率", "rated frequency", 16);
        Require(Int(rated, "decimal_attribute_raw") == 0 && Int(maximum, "decimal_attribute_raw") == 1 && Int(frequency, "decimal_attribute_raw") == 2,
            "Motor rating decimal format differs from the inspected profile.");
        Require(Str(rated, "unit") == "rpm" && Str(maximum, "unit") == "%" && Str(frequency, "unit") == "Hz",
            "Motor rating units differ from the inspected profile.");
        EqualFields(MotorFingerprint(), limits, "Motor configuration changed during discovery");
        limits["effective_maximum_rpm"] = Math.Min(Number(limits, "rated_rpm"), Number(limits, "motor_max_rpm"));
        limits["rating_parameter_code"] = "D" + (Int(limits, "motor_data_group") * 3).ToString("D2") + ".05";
        limits["rating_parameter_name"] = rated["name"];
        limits["basis"] = "inspected profile with live active motor group and rated/max/frequency metadata";
        return limits;
    }
    private static void CheckMotor(Dictionary<string, object> expected)
    { EqualFields(MotorFingerprint(), expected, "Motor selection/rating changed; motion plan invalidated"); }
    private static long SignedPosition(byte group, byte offset)
    {
        byte[] values = ParameterFrame(0x55CC, 0xFB, group, offset, 2, 3);
        return unchecked((int)((uint)BitConverter.ToUInt16(values, 6) | (uint)BitConverter.ToUInt16(values, 9) << 16));
    }
    private static string PositionFingerprint()
    {
        byte[] values = ParameterFrame(0x55CC, 0xFB, 0x80, 0, 71, 3);
        var words = new List<string>();
        for (int i = 0; i < 71; i++) words.Add(BitConverter.ToUInt16(values, 6 + i * 3).ToString("X4"));
        return String.Join("", words.ToArray());
    }
    private static Dictionary<string, object> PositionState()
    {
        var feedback = NamedDefinition(0xEE, 22, "位置反馈", "position feedback", 32);
        Require(Convert.ToBoolean(feedback["signed"]) && Int(feedback, "decimal_attribute_raw") == 0, "Position feedback is not signed command pulses.");
        var displacement = NamedDefinition(0x83, 20, "移动位移", "displacement", 32);
        var speed = NamedDefinition(0x83, 22, "最大速度", "maximum speed", 16);
        var acceleration = NamedDefinition(0x83, 23, "加减速", "acceleration", 16);
        Require(Convert.ToBoolean(displacement["signed"]) && Int(displacement, "decimal_attribute_raw") == 0 && Int(speed, "decimal_attribute_raw") == 0 && Int(acceleration, "decimal_attribute_raw") == 0,
            "Position JOG formats differ from the inspected profile.");
        Require(Str(speed, "unit") == "rpm" && Str(acceleration, "unit") == "ms", "Position JOG units differ from the inspected profile.");
        var mode = Definition(0x35, 1); var jog = Definition(0x30, 22);
        return new Dictionary<string, object> { { "position_pulses", SignedPosition(0xEE, 22) }, { "position_code", "P14.22" },
            { "position_fingerprint", PositionFingerprint() }, { "state_raw", (int)Word(0x20, 0) }, { "status_word", (int)Word(0xEC, 7) },
            { "motor_limits", MotionLimits() }, { "settings", new object[] { mode, jog, displacement, speed, acceleration } },
            { "physical_zero_verified", false } };
    }
    private static MotionSession PrepareMotion(Dictionary<string, object> input)
    {
        var limits = MotionLimits();
        if (input.ContainsKey("expected_motion_limits")) CheckMotor((Dictionary<string, object>)input["expected_motion_limits"]);
        var result = new MotionSession { Raw = Int(input, "speed_raw"), MaximumRaw = Int(input, "max_speed_raw"), Unit = Str(input, "speed_unit"), Limits = limits };
        Require(result.Unit == "rpm" || result.Unit == "Hz", "Use rpm or Hz.");
        Require(!input.ContainsKey("serial_binding_id") || result.Unit == "rpm", "CM55 control currently supports rpm only.");
        result.Scale = 100; result.Idle = result.Unit == "rpm" ? (ushort)0x947E : (ushort)0x8C7E;
        if (input.ContainsKey("speed_encoding"))
        {
            var encoding = (Dictionary<string, object>)input["speed_encoding"];
            string wire = Str(encoding, "wire_unit"); int divisor = Int(encoding, "divisor");
            double basis = result.Unit == "rpm" ? Number(limits, "base_rpm") : Number(limits, "rated_frequency_raw") / 100.0;
            if (wire == "percent")
            {
                Require(divisor == 10000 || divisor == 100000, "Unsupported percentage divisor.");
                result.Scale = divisor / basis; result.Idle = divisor == 100000 ? (ushort)0xA47E : (ushort)0x847E;
            }
            else
            {
                Require(wire == result.Unit && (divisor == 100 || divisor == 1000), "Invalid direct speed encoding.");
                result.Scale = divisor; if (divisor == 1000) result.Idle |= 0x2000;
            }
            Require(Int(encoding, "control_idle") == result.Idle && Int(encoding, "raw") == result.Raw && Int(encoding, "maximum_raw") == result.MaximumRaw,
                "Speed encoding/value contract mismatch.");
        }
        result.Maximum = input.ContainsKey("maximum_abs_speed") ? Number(input, "maximum_abs_speed") : result.MaximumRaw / result.Scale;
        double allowed = Number(limits, "effective_maximum_rpm");
        if (result.Unit == "Hz") allowed = allowed / Number(limits, "base_rpm") * Number(limits, "rated_frequency_raw") / 100;
        Require(result.Maximum > 0 && result.Maximum <= allowed + 1e-9 && result.MaximumRaw > 0 && result.MaximumRaw <= 32767 &&
            result.Raw != 0 && Math.Abs((long)result.Raw) <= result.MaximumRaw && result.MaximumRaw / result.Scale <= result.Maximum + 1e-9,
            "Run exceeds live motor rating or authorized speed limits.");
        result.Setpoint = result.Raw / result.Scale; result.InitialDirection = Math.Sign(result.Raw);
        result.AllowDirectionChange = input.ContainsKey("allow_direction_change") && Convert.ToBoolean(input["allow_direction_change"]);
        return result;
    }
    private static object MotionState()
    {
        Require(motion != null, "No active motion session.");
        return new { control_revision = motion.Revision, speed_setpoint = motion.Setpoint, speed_unit = motion.Unit,
            maximum_abs_speed = motion.Maximum, position_move = motion.Position, speed_raw = motion.Raw };
    }
    private static object UpdateSpeed(Dictionary<string, object> input)
    {
        Require(motion != null && !motion.Position, "Speed adjustment requires an active speed session.");
        Require(Int(input, "expected_control_revision") == motion.Revision, "Speed changed since preview; create a new preview.");
        CheckMotor(motion.Limits);
        Require(Word(0x35, 1) == 0, "Drive left speed mode.");
        Require((Word(0xEC, 7) & 0x020C) == 0x0204, "PC running ownership is not verified; no speed update sent.");
        int raw = Int(input, "speed_raw");
        Require(Math.Abs((long)raw) <= motion.MaximumRaw && (raw == 0 || motion.AllowDirectionChange || Math.Sign(raw) == motion.InitialDirection),
            "Requested speed/direction exceeds this run's authorization.");
        Require(Math.Abs(raw / motion.Scale - Number(input, "effective_speed")) < 1e-7, "Speed update encoding mismatch.");
        // Keep the session's unit/decimal bits unchanged; firmware handles its ramp.
        Write(0xEC, 14, unchecked((ushort)raw), false);
        if (Word(0xEC, 14) != unchecked((ushort)raw)) { runFault = true; throw new IOException("Speed write readback differs; no retry."); }
        motion.Raw = raw; motion.Setpoint = raw / motion.Scale; motion.Revision++;
        Emit(new { phase = "speed_changed", control_revision = motion.Revision, speed_setpoint = motion.Setpoint, speed_unit = motion.Unit });
        return new { speed_command_acknowledged = true, setpoint_readback_verified = true, control_revision = motion.Revision,
            speed_setpoint = motion.Setpoint, speed_unit = motion.Unit, actual_speed_reached = false, external_ramp_generated = false };
    }
    private static object[] CheckPositionPlan(Dictionary<string, object> input)
    {
        var before = (Dictionary<string, object>)input["position_before"]; var live = PositionState();
        Require(Str(before, "position_fingerprint") == Str(live, "position_fingerprint"), "Position coordinate/gearing configuration changed.");
        long start = Convert.ToInt64(input["start_position_pulses"]), end = Convert.ToInt64(input["target_position_pulses"]), delta = Convert.ToInt64(input["displacement_pulses"]);
        Require(Convert.ToInt64(live["position_pulses"]) == start && delta != 0 && delta >= Int32.MinValue && delta <= Int32.MaxValue && start + delta == end,
            "Position changed since preview or displacement overflows; no motion sent.");
        Require(start >= Convert.ToInt64(input["minimum_position_pulses"]) && start <= Convert.ToInt64(input["maximum_position_pulses"]) &&
            end >= Convert.ToInt64(input["minimum_position_pulses"]) && end <= Convert.ToInt64(input["maximum_position_pulses"]), "Position is outside the authorized travel range.");
        Require(Word(0x80, 1) == 0 && Word(0x80, 14) == 0 && Word(0x80, 22) == 0,
            "Position adapter requires fixed command direction, fixed gearing selection and normal motor direction.");
        Require(Int(input, "position_tolerance_pulses") >= 0, "Invalid position tolerance.");
        object[] old = ((ArrayList)before["settings"]).ToArray();
        byte[] groups = { 0x35, 0x30, 0x83, 0x83, 0x83 }, offsets = { 1, 22, 20, 22, 23 };
        Require(old.Length == 5, "Position settings contract mismatch.");
        uint[] desired = { 2, 1, unchecked((uint)(int)delta), (uint)Number(input, "position_speed_rpm"), (uint)Int(input, "acceleration_ms") };
        Require(Number(input, "position_speed_rpm") == Math.Truncate(Number(input, "position_speed_rpm")) && desired[3] > 0 && desired[3] <= motion.Maximum &&
            Int(input, "acceleration_ms") >= 0 && Int(input, "acceleration_ms") <= 65535, "Invalid JOG speed/acceleration.");
        var settings = new List<object>();
        for (int i = 0; i < 5; i++)
        {
            var expected = (Dictionary<string, object>)old[i]; var current = Definition(groups[i], offsets[i]);
            foreach (string key in new string[] { "attribute_raw", "raw_value", "name", "decimal_attribute_raw", "lower_raw", "upper_raw", "width_bits" })
                Require(Convert.ToString(expected[key]) == Convert.ToString(current[key]), "Position setting changed since preview: " + key);
            Require(Int(current, "access") == 0 || Int(current, "access") == 1, "Position setting is not writable.");
            bool signed = Convert.ToBoolean(current["signed"]), wide = Int(current, "width_bits") == 32;
            long value = signed ? (wide ? (long)unchecked((int)desired[i]) : (long)unchecked((short)desired[i])) : desired[i];
            if (!Convert.ToBoolean(current["limit_is_reference"]))
            {
                long low = signed ? (long)unchecked((int)Convert.ToUInt32(current["lower_raw"])) : Convert.ToUInt32(current["lower_raw"]);
                long high = signed ? (long)unchecked((int)Convert.ToUInt32(current["upper_raw"])) : Convert.ToUInt32(current["upper_raw"]);
                Require(value >= low && value <= high, "Position setting exceeds live device limits.");
            }
            settings.Add(new Dictionary<string, object> { { "group", (int)groups[i] }, { "offset", (int)offsets[i] }, { "before", current["raw_value"] },
                { "desired", desired[i] }, { "wide", wide } });
        }
        return settings.ToArray();
    }
    private static void WritePositionSettings(object[] settings, bool restore)
    {
        foreach (Dictionary<string, object> setting in settings)
        {
            byte group = (byte)Int(setting, "group"), offset = (byte)Int(setting, "offset");
            uint before = Convert.ToUInt32(setting["before"]), desired = Convert.ToUInt32(setting["desired"]);
            if (before == desired) continue;
            Require((Word(0xEC, 7) & 4) == 0 && (Word(0x20, 0) == 4 || Word(0x20, 0) == 5 || Word(0x20, 0) == 1), "Position settings require a verified stopped state.");
            var current = Definition(group, offset); uint raw = Convert.ToUInt32(current["raw_value"]);
            if (restore && raw == before) continue;
            Require(raw == (restore ? desired : before), "Position setting changed externally; no overwrite attempted.");
            uint value = restore ? before : desired;
            Write(group, offset, value, Convert.ToBoolean(setting["wide"]));
            Require(Convert.ToUInt32(Definition(group, offset)["raw_value"]) == value, "Position setting readback differs; no retry.");
        }
    }
    private static void ProcessWorkerCommand(Dictionary<string, object> run, Dictionary<string, object> identity, bool priorityOnly = false)
    {
        QueuedCommand command;
        if (commandQueue == null) return;
        if (!controlQueue.TryTake(out command) && (priorityOnly || !commandQueue.TryTake(out command))) return;
        bool priorProcessing = processingControl;
        if (command.Input != null && (Str(command.Input, "operation") == "update_speed" || Str(command.Input, "operation") == "motion_state")) processingControl = true;
        string requestPath = command.Path, resultPath = requestPath.Replace(".request.json", ".result.json");
        object response; string operation = "invalid";
        try
        {
            Require(command.Error == null, "Invalid queued request: " + command.Error);
            var input = command.Input;
            operation = Str(input, "operation");
            Require(Str(input, "expected_mac") == Str(identity, "mac") && Str(input, "run_job_id") == Str(run, "run_job_id"), "Queued command belongs to another device/run.");
            Require(operation == "identity" || operation == "read" || operation == "catalog" || operation == "capture" || operation == "snapshot" ||
                operation == "motion_limits" || operation == "motion_state" || operation == "position_state" || operation == "update_speed", "Unsupported worker operation.");
            Require(!run.ContainsKey("serial_binding_id") || (input.ContainsKey("serial_binding_id") && Str(input, "serial_binding_id") == Str(run, "serial_binding_id")), "Queued command belongs to another serial binding.");
            var live = BoundIdentity(Str(run, "transport") == "tcp", run);
            foreach (string key in live.Keys) Require(Convert.ToString(live[key]) == Convert.ToString(identity[key]), "Run parameter identity changed.");
            Require(Word(0x22, 0) == Int(identity, "software_raw") && Word(0x22, 8).ToString("X4") == Str(identity, "release_hex"), "Run firmware changed.");
            CheckExpectedIdentity(input, identity);
            object[] addresses = input.ContainsKey("parameters") ? ((ArrayList)input["parameters"]).ToArray() : new object[0];
            Require(addresses.Length <= (operation == "snapshot" ? 4000 : 20), "Too many queued parameters.");
            object result;
            if (operation == "identity") result = new { identity_verified = true };
            else if (operation == "motion_limits") result = MotionLimits();
            else if (operation == "motion_state") result = MotionState();
            else if (operation == "position_state") result = PositionState();
            else if (operation == "update_speed") result = UpdateSpeed(input);
            else if (operation == "catalog") result = DiscoverCatalog(input);
            else if (operation == "capture") result = Capture(input, addresses);
            else if (operation == "snapshot") result = SnapshotValues(addresses);
            else
            {
                var parameters = new List<object>();
                foreach (Dictionary<string, object> a in addresses)
                {
                    int g = Int(a, "group"), o = Int(a, "offset"); Require(g >= 0x20 && g <= 0xEF && o >= 0 && o < 100, "Invalid queued address.");
                    var p = Definition((byte)g, (byte)o); p["code"] = Str(a, "code"); parameters.Add(p);
                }
                result = new { parameters = parameters };
            }
            if (operation == "catalog" || operation == "snapshot")
            {
                var after = BoundIdentity(Str(run, "transport") == "tcp", run);
                foreach (string key in after.Keys) Require(Convert.ToString(after[key]) == Convert.ToString(identity[key]), "Parameter identity changed during queued operation.");
                Require(Word(0x22, 0) == Int(identity, "software_raw") && Word(0x22, 8).ToString("X4") == Str(identity, "release_hex"), "Firmware changed during queued operation.");
            }
            response = new { status = "passed", operation = operation, mac = identity["mac"], identity = identity,
                software_raw = identity["software_raw"], release_hex = identity["release_hex"], state_raw = Word(0x20, 0),
                original_sha256 = VendorSha, result = result, device_write_commands_sent = operation == "capture" || operation == "update_speed",
                physical_unique_identity_verified = !run.ContainsKey("serial_binding_id"),
                background_control_writes_continued = true, executed_on_run_connection = true, run_job_id = run["run_job_id"], connection_closed_after_operation = false };
        }
        catch (Exception error)
        { response = new { status = error is OperationCanceledException ? "cancelled" : "failed", operation = operation, error = error.Message, executed_on_run_connection = true }; }
        object completed = response;
        Interlocked.Increment(ref pendingCommandResults);
        ThreadPool.QueueUserWorkItem(delegate {
            try { string temporary = resultPath + ".tmp";
                var serializer = new JavaScriptSerializer { MaxJsonLength = 8000000 };
                File.WriteAllText(temporary, serializer.Serialize(completed), new UTF8Encoding(false)); File.Move(temporary, resultPath); File.Delete(requestPath); }
            catch (Exception) { logFailed = true; }
            finally { Interlocked.Decrement(ref pendingCommandResults); }
        });
        processingControl = priorProcessing;
        if (transportFailed || runFault) throw new IOException("Run communication or drive fault during queued operation; stopping.");
    }
    private static object RunBounded(Dictionary<string, object> input, Dictionary<string, object> identity)
    {
        int duration = Int(input, "duration_ms");
        bool position = Str(input, "operation") == "position_move";
        bool continuous = Str(input, "operation") == "run_continuous" || position;
        int maximumDuration = input.ContainsKey("maximum_duration_ms") ? Int(input, "maximum_duration_ms") : 0;
        Require(maximumDuration >= 0 && ((continuous && duration == 0) || duration > 0), "Invalid run duration.");
        motion = PrepareMotion(input);
        motion.Position = position;
        string unit = motion.Unit;
        object[] positionSettings = position ? CheckPositionPlan(input) : null;
        Require(!continuous || input.ContainsKey("stop_file"), "Continuous run requires a stop-request file.");
        Require(position || Word(0x35, 1) == 0, "B05.01 is not speed control. Make an explicit mode-change preview before attempting a speed run.");
        Require((Word(0xEC, 7) & 0x0204) == 0, "Device is already running or controlled locally; no takeover attempted.");
        Require(Word(0x20, 0) == 4 || Word(0x20, 0) == 5, "Drive must be in start-inhibit or switch-on-ready state.");
        ushort idle = motion.Idle; // PC heartbeat + control request + fixed units, OFF1=0.
        var states = new List<object>(); bool entered = false, stopped = false, modeRestored = false, settingsStarted = false;
        bool positionReached = false, jogSent = false; int settled = 0; long jogAt = 0, finalPosition = 0;
        ushort runningControl = (ushort)(idle | 1);
        StartRunServices(input);
        // JIT the queue handler before sending a run-enable command.
        ProcessWorkerCommand(input, identity);
        try
        {
            if (position) { settingsStarted = true; WritePositionSettings(positionSettings, false); }
            entered = true;
            Write(0xEC, 14, 0, false); Write(0xEC, 11, idle, false); Thread.Sleep(100);
            Write(0xEC, 11, idle, false); Thread.Sleep(100);
            Require((Word(0xEC, 7) & 0x0200) != 0, "PC control request was not acquired; run was not sent.");
            var feedbackDefinition = Definition(0x20, 6);
            Write(0xEC, 14, position ? 0u : unchecked((ushort)motion.Raw), false);
            Stopwatch clock = Stopwatch.StartNew();
            long nextReport = 0, nextHeartbeat = 0; bool sendingHeartbeat = false, reporting = false;
            responseDeadlineMs = 200;
            serviceRun = delegate {
                if (sendingHeartbeat) return;
                if (!suppressCancellation && positionReached) throw new OperationCanceledException("Target position reached; queued operation cancelled.");
                if (!suppressCancellation && maximumDuration > 0 && clock.ElapsedMilliseconds >= maximumDuration) throw new OperationCanceledException("Background run duration reached; queued operation cancelled.");
                if (logFailed) { runFault = true; throw new IOException("Run journal or queued-result output failed; stopping."); }
                if (!suppressCancellation && input.ContainsKey("stop_file") && File.Exists(Str(input, "stop_file"))) throw new OperationCanceledException("Run stop requested; queued operation cancelled.");
                if (clock.ElapsedMilliseconds >= nextHeartbeat)
                {
                    sendingHeartbeat = true;
                    try { watchdog.Change(60000, Timeout.Infinite); Write(0xEC, 11, runningControl, false); nextHeartbeat = clock.ElapsedMilliseconds + 80; }
                    finally { sendingHeartbeat = false; }
                }
                if (!reporting && clock.ElapsedMilliseconds >= nextReport)
                {
                    reporting = true; nextReport = clock.ElapsedMilliseconds + (position ? 100 : 1000);
                    try
                    {
                        ushort state = Word(0x20, 0), status = Word(0xEC, 7), feedbackRaw = Word(0x20, 6);
                        if (state == 3 || state == 27 || (status & 8) != 0) { runFault = true; throw new InvalidOperationException("Drive fault while running."); }
                        CheckMotor(motion.Limits);
                        if (Word(0x35, 1) != (position ? 2 : 0) || (position && Word(0x30, 22) != 1) || (status & 0x200) == 0)
                        { runFault = true; throw new InvalidOperationException("Control mode/JOG mode/PC ownership changed while running."); }
                        long decoded = Convert.ToBoolean(feedbackDefinition["signed"]) ? (long)unchecked((short)feedbackRaw) : feedbackRaw;
                        double feedback = decoded / Math.Pow(10, Int(feedbackDefinition, "decimal_attribute_raw"));
                        if (unit == "rpm" && Convert.ToString(feedbackDefinition["unit"]) == "rpm" &&
                            Math.Abs(feedback) > motion.Maximum + Math.Max(10, motion.Maximum / 10))
                        { runFault = true; throw new InvalidOperationException("RPM feedback exceeded requested range or direction; stopping."); }
                        if (position)
                        {
                            if (!jogSent && state == 12 && (status & 4) != 0)
                            { runningControl |= 0x100; jogSent = true; jogAt = clock.ElapsedMilliseconds; nextHeartbeat = 0; }
                            finalPosition = SignedPosition(0xEE, 22);
                            if (finalPosition < Convert.ToInt64(input["minimum_position_pulses"]) || finalPosition > Convert.ToInt64(input["maximum_position_pulses"]))
                            { runFault = true; throw new InvalidOperationException("Position feedback left the authorized travel range."); }
                            if (Str(input, "position_fingerprint") != PositionFingerprint())
                            { runFault = true; throw new InvalidOperationException("Coordinate/gearing configuration changed while positioning."); }
                            bool reached = jogSent && clock.ElapsedMilliseconds - jogAt >= 300 && SignedPosition(0xEE, 66) == Convert.ToInt64(input["displacement_pulses"]) &&
                                Math.Abs(finalPosition - Convert.ToInt64(input["target_position_pulses"])) <= Int(input, "position_tolerance_pulses") &&
                                Word(0xE1, 91) != 0 && Word(0xE1, 38) != 0 && Math.Abs(feedback) <= 1;
                            settled = reached ? settled + 1 : 0; positionReached = settled >= 3;
                            Emit(new { phase = "position_progress", position_pulses = finalPosition, target_position_pulses = input["target_position_pulses"],
                                position_reached = positionReached, jog_trigger_sent = jogSent });
                        }
                        var sample = new { phase = "run_progress", elapsed_ms = clock.ElapsedMilliseconds, state_raw = state, status_word = status,
                            run_bit = (status & 4) != 0, speed_setpoint = motion.Setpoint, speed_unit = unit, control_revision = motion.Revision,
                            feedback_code = "A00.06", feedback_value = feedback, feedback_unit = feedbackDefinition["unit"] };
                        states.Add(sample); if (states.Count > 100) states.RemoveAt(0); Emit(sample);
                    }
                    finally { reporting = false; }
                }
                if (!processingControl && !suppressCancellation) ProcessWorkerCommand(input, identity, true);
            };
            while ((continuous && (maximumDuration == 0 || clock.ElapsedMilliseconds < maximumDuration)) || (!continuous && clock.ElapsedMilliseconds < duration))
            {
                if (positionReached || (input.ContainsKey("stop_file") && File.Exists(Str(input, "stop_file")))) break;
                try { serviceRun(); ProcessWorkerCommand(input, identity); }
                catch (OperationCanceledException) { break; }
                Thread.Sleep(5);
            }
        }
        finally
        {
            serviceRun = null; responseDeadlineMs = 3000; commandReaderStopped = true;
            if (entered)
            {
                // OFF1 stop while retaining heartbeat, then verify the run bit
                // clears before releasing control. A failed stop is never success.
                Write(0xEC, 14, 0, false);
                Stopwatch stopWait = Stopwatch.StartNew();
                do { Write(0xEC, 11, idle, false); Thread.Sleep(100); stopped = (Word(0xEC, 7) & 4) == 0; }
                while (!stopped && stopWait.ElapsedMilliseconds < 10000);
                if (!stopped) Write(0xEC, 11, 0x8000, false);
                Require(stopped, "Stop was not verified within 10 seconds; use the local stop and inspect the drive.");
                Write(0xEC, 11, 0x8000, false);
                if (input.ContainsKey("restore_control_mode"))
                {
                    int restore = Int(input, "restore_control_mode");
                    Require(restore >= 0 && restore <= 2 && Word(0x35, 1) == 0, "Control mode changed externally; original mode was not restored automatically.");
                    Write(0x35, 1, (uint)restore, false);
                    Require(Word(0x35, 1) == restore, "Restoring B05.01 was not verified.");
                    modeRestored = true;
                }
            }
            if (settingsStarted)
            {
                Require(!entered || stopped, "Position settings restoration deferred because stop is unverified.");
                CheckExpectedIdentity(input, BoundIdentity(Str(input, "transport") == "tcp", input));
                WritePositionSettings(positionSettings, true); modeRestored = true;
            }
        }
        return new { speed_setpoint = motion.Setpoint, speed_unit = unit, control_revision = motion.Revision, duration_ms = duration, continuous = continuous, states = states, run_command_sent = true,
                     motion_measured = false, stopped_run_bit_verified = stopped, control_released = true,
                     position_move = position, position_reached = positionReached, final_position_pulses = finalPosition,
                     termination_reason = positionReached ? "target_reached" : input.ContainsKey("stop_file") && File.Exists(Str(input, "stop_file")) ? "stop_requested" : "duration_elapsed",
                     position_settings_restored = settingsStarted && modeRestored,
                     control_mode_restore_requested = input.ContainsKey("restore_control_mode"), control_mode_restored = modeRestored };
    }
    private static object StopPC()
    {
        ushort status = Word(0xEC, 7);
        Require((status & 0x0200) != 0, "Drive is not under local/PC control; this tool cannot stop a remote PLC command.");
        ushort idle = (ushort)(0x847E | (Word(0xEC, 11) & 0x3800));
        Write(0xEC, 14, 0, false); Stopwatch clock = Stopwatch.StartNew(); bool stopped;
        do { Write(0xEC, 11, idle, false); Thread.Sleep(100); stopped = (Word(0xEC, 7) & 4) == 0; }
        while (!stopped && clock.ElapsedMilliseconds < 10000);
        if (!stopped) Write(0xEC, 11, 0x8000, false);
        Require(stopped, "PC stop was not verified within 10 seconds; use the local stop.");
        Write(0xEC, 11, 0x8000, false);
        return new { stop_command_acknowledged = true, stopped_run_bit_verified = true, control_released = true,
                     state_after = Word(0x20, 0), stopped_motion_verified = false };
    }
    private static object ResetFault()
    {
        ushort state = Word(0x20, 0), status = Word(0xEC, 7);
        Require((status & 4) == 0, "Drive is running; fault reset was not sent.");
        if (state != 3 && state != 27 && (status & 8) == 0)
            return new { fault_reset_sent = false, fault_cleared_verified = true, state_after = state, run_command_sent = false };
        // PC ackFault is bit 7; no control request or run-enable bits are set.
        Write(0xEC, 14, 0, false);
        Write(0xEC, 11, 0x8000, false); Thread.Sleep(100);
        Write(0xEC, 11, 0x8080, false); Thread.Sleep(100);
        Write(0xEC, 11, 0x8000, false);
        Stopwatch wait = Stopwatch.StartNew();
        do { Thread.Sleep(100); state = Word(0x20, 0); status = Word(0xEC, 7); }
        while ((state == 3 || state == 27 || (status & 8) != 0) && wait.ElapsedMilliseconds < 2000);
        Require(state != 3 && state != 27 && (status & 12) == 0, "Fault reset was sent but a stopped, fault-free status was not verified.");
        return new { fault_reset_sent = true, fault_cleared_verified = true, state_after = state, status_word_after = status, run_command_sent = false };
    }
    private static object Capture(Dictionary<string, object> input, object[] addresses)
    {
        int sample = Int(input, "sample_divisor"), points = Int(input, "points");
        Require(addresses.Length >= 1 && addresses.Length <= 4 && sample >= 1 && sample <= 500 && points >= 1 && points <= 20000, "Invalid capture limits.");
        byte[] setup = new byte[16]; setup[0] = 0xEE; setup[1] = 0x55; setup[2] = 0xFA;
        setup[3] = (byte)sample; setup[4] = (byte)(sample >> 8); setup[5] = (byte)addresses.Length;
        var definitions = new List<object>();
        for (int i = 0; i < addresses.Length; i++)
        {
            var a = (Dictionary<string, object>)addresses[i]; byte g = (byte)Int(a, "group"), o = (byte)Int(a, "offset");
            Require(g >= 0xE0 && g <= 0xEF && o < 100, "Scope requires P group channels.");
            var d = Definition(g, o); Require(Int(d, "width_bits") == 16 && Int(d, "access") != 3, "This capture supports readable 16-bit channels.");
            d["code"] = Str(a, "code"); definitions.Add(d); setup[6 + 2 * i] = g; setup[7 + 2 * i] = o;
        }
        var rows = new List<ushort[]>(); bool started = false, stopped = false;
        try
        {
            // E2 stop clears old samples before configuring a new E1 acquisition.
            byte[] stop = Exchange(new byte[] { 0xEE, 0x55, 0xFB, 0, 0, 0 }, 7, true);
            Require(stop[3] == 0 && stop[4] == 1, "Scope did not acknowledge clearing previous data.");
            started = true;
            byte[] accepted = Exchange(setup, 6, true); Require(accepted[3] == 0, "Scope configuration rejected.");
            Stopwatch wait = Stopwatch.StartNew();
            while (rows.Count < points)
            {
                Require(wait.ElapsedMilliseconds < Int(input, "timeout_ms"), "Capture deadline exceeded; buffer may have overflowed.");
                byte[] data = ScopePoll(addresses.Length);
                for (int j = 0; j < data[5] && rows.Count < points; j++)
                { ushort[] row = new ushort[addresses.Length]; for (int c = 0; c < row.Length; c++) row[c] = BitConverter.ToUInt16(data, 6 + 2 * (j * row.Length + c)); rows.Add(row); }
                Thread.Sleep(5);
            }
        }
        finally
        {
            if (started)
            {
                suppressCancellation = true;
                try { byte[] response = Exchange(new byte[] { 0xEE, 0x55, 0xFB, 0, 0, 0 }, 7, true);
                    Require(response[3] == 0 && response[4] == 1, "Scope stop acknowledgement missing."); stopped = true; }
                finally { suppressCancellation = false; }
            }
        }
        return new { definitions = definitions, raw_samples = rows, points = rows.Count, scope_stopped = stopped,
                     sample_divisor = sample, trigger = "immediate", timing_source = "firmware 2ms task multiplied by E1 sample divisor", dropped_samples_detectable = false };
    }
    private static object Operate(Dictionary<string, object> input, Dictionary<string, object> identity, bool tcp)
    {
        string operation = Str(input, "operation");
        bool cm55 = input.ContainsKey("device_profile") && Str(input, "device_profile") == "cm55_ec_612";
        string actual = cm55 ? null : Str(identity, "mac");
        ushort release = Convert.ToUInt16(Str(identity, "release_hex"), 16), software = (ushort)Int(identity, "software_raw"), state = Word(0x20, 0);
        if (operation != "identity" && operation != "catalog" && operation != "read" && operation != "snapshot" && operation != "motion_limits" && operation != "position_state")
        {
            bool inspectedBuild = cm55 ? software == 612 && release == 0x6629 : software == 910 && (release == 0x6908 || release == 0x6A22);
            string captureProfile = cm55 ? "cm55_ec_spi_612" : "an_pn_spi_910";
            bool registeredCapture = operation == "capture" && software == (cm55 ? 612 : 910) && input.ContainsKey("expected_identity") &&
                input.ContainsKey("registered_capture_profile") && Str(input, "registered_capture_profile") == captureProfile;
            Require(inspectedBuild || registeredCapture, "Writes/control require inspected AN builds or CM55 EC 6.12/6629; new-build capture requires a registered boot-verified profile.");
        }
        CheckExpectedIdentity(input, identity);
        if (input.ContainsKey("expected_release")) Require(release.ToString("X4") == Str(input, "expected_release"), "Firmware changed since preview; no write sent.");
        object[] addresses = input.ContainsKey("parameters") ? ((ArrayList)input["parameters"]).ToArray() : new object[0];
        Require(addresses.Length <= (operation == "snapshot" ? 4000 : 20), "Too many parameters per operation.");
        object result;
        if (operation == "catalog")
        {
            result = DiscoverCatalog(input);
            var after = BoundIdentity(tcp, input);
            foreach (string key in after.Keys) Require(Convert.ToString(after[key]) == Convert.ToString(identity[key]), "Device parameter identity changed during discovery; catalog discarded.");
            Require(Word(0x22, 0) == software && Word(0x22, 8) == release, "Firmware changed during catalog discovery; catalog discarded.");
        }
        else if (operation == "identity") result = new { identity_verified = true };
        else if (operation == "motion_limits") result = MotionLimits();
        else if (operation == "position_state") result = PositionState();
        else if (operation == "snapshot")
        {
            result = SnapshotValues(addresses);
            var after = BoundIdentity(tcp, input);
            foreach (string key in after.Keys) Require(Convert.ToString(after[key]) == Convert.ToString(identity[key]), "Parameter identity changed during snapshot; snapshot discarded.");
            Require(Word(0x22, 0) == software && Word(0x22, 8) == release, "Firmware changed during snapshot; snapshot discarded.");
        }
        else if (operation == "capture") result = Capture(input, addresses);
        else if (operation == "run" || operation == "run_continuous" || operation == "position_move") result = RunBounded(input, identity);
        else if (operation == "stop") result = StopPC();
        else if (operation == "reset_fault") result = ResetFault();
        else
        {
            var values = new List<object>();
            foreach (Dictionary<string, object> a in addresses)
            {
                int g = Int(a, "group"), o = Int(a, "offset"); Require(g >= 0x20 && g <= 0xEF && o >= 0 && o < 100, "Invalid parameter address.");
                var d = Definition((byte)g, (byte)o);
                if (operation == "write")
                {
                    Require(addresses.Length == 1 && (Int(d, "access") == 0 || Int(d, "access") == 1), "Use dedicated tools for command or read-only registers.");
                    Require(g < 0xE0 && !(g == 0x24 && (o == 31 || o == 32)), "Use dedicated control/restart tools for command registers.");
                    Require(Convert.ToUInt32(a["expected_before"]) == Convert.ToUInt32(d["raw_value"]) && Convert.ToUInt32(a["expected_attribute"]) == Convert.ToUInt32(d["attribute_raw"]), "Parameter changed since preview; no write sent.");
                    if (a.ContainsKey("expected_name")) Require(Str(a, "expected_name") == Str(d, "name") && Int(a, "expected_decimal") == Int(d, "decimal_attribute_raw") && Convert.ToUInt32(a["expected_lower"]) == Convert.ToUInt32(d["lower_raw"]) && Convert.ToUInt32(a["expected_upper"]) == Convert.ToUInt32(d["upper_raw"]), "Parameter definition changed since preview; no write sent.");
                    Require(state == 4 || state == 5 || state == 1, "This initial write adapter requires a stopped drive state.");
                    Write((byte)g, (byte)o, Convert.ToUInt32(a["raw_value"]), Int(d, "width_bits") == 32);
                    Thread.Sleep(100); d = Definition((byte)g, (byte)o);
                    Require(Convert.ToUInt32(d["raw_value"]) == Convert.ToUInt32(a["raw_value"]), "Write acknowledged but readback differs; do not retry automatically.");
                }
                d["code"] = a["code"]; values.Add(d);
            }
            result = new { parameters = values };
        }
        return new { status = "passed", operation = operation, mac = actual, software_raw = software, release_hex = release.ToString("X4"), state_raw = state,
                   identity = identity, physical_unique_identity_verified = !cm55, original_sha256 = VendorSha, result = result, device_write_commands_sent = writeAttempted, frames = frames, connection_closed_after_operation = true };
    }
    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        watchdog = new Timer(delegate { Emit(new { status = "failed", error = "Device bridge made no progress for 60 seconds.", device_write_commands_sent = writeAttempted }); Environment.Exit(2); }, null, 60000, Timeout.Infinite);
        try
        {
            Require(args.Length == 2, "Usage: ReuseDeviceBridge vendor-exe request-json");
            var input = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[1], Encoding.UTF8));
            if (input.ContainsKey("run_journal")) runJournal = new StreamWriter(new FileStream(Str(input, "run_journal"), FileMode.Create, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false));
            string operation = Str(input, "operation");
            bool inspectSerial = operation == "inspect_identity" || operation == "inspect_catalog" || operation == "inspect_read";
            Require(!inspectSerial || (Str(input, "transport") == "serial" && System.Text.RegularExpressions.Regex.IsMatch(Str(input, "port"), "^COM[1-9][0-9]{0,3}$")), "Inspection requires an explicit Windows serial port.");
            bool cm55 = input.ContainsKey("device_profile") && Str(input, "device_profile") == "cm55_ec_612";
            Require(!cm55 || Str(input, "transport") == "serial", "CM55 profile supports serial only.");
            string mac = Str(input, "transport") == "serial" ? "Serial-" + Str(input, "port") : Str(input, "expected_mac");
            Require(inspectSerial || operation == "snapshot" || operation == "catalog" || operation == "identity" || operation == "read" || operation == "write" || operation == "capture" || operation == "stop" || operation == "run" || operation == "run_continuous" || operation == "reset_fault" || operation == "motion_limits" || operation == "position_state" || operation == "position_move", "Unknown operation.");
            deviceLock = new Mutex(false, "Local\\BonengFirmwareUpgrade-" + mac.Replace(":", ""));
            try { ownsLock = deviceLock.WaitOne(0); } catch (AbandonedMutexException) { ownsLock = true; }
            Require(ownsLock, "Another operation holds this device.");
            using (SHA256 sha = SHA256.Create())
                Require(BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(args[0]))).Replace("-", "").ToLowerInvariant() == VendorSha, "Vendor application hash mismatch.");
            vendor = Assembly.LoadFrom(Path.GetFullPath(args[0])); commType = T("communication.CCommModbus"); entityType = T("communication.EntityModbus"); actionType = T("communication.IAction");
            object form = Call(T("form.Form2"), null, "getInstance", Type.EmptyTypes); Type ft = form.GetType();
            ft.GetField("Control_comm").SetValue(form, 1); ft.GetField("Circle_comm").SetValue(form, 80);
            comm = Call(commType, null, "getInstance", Type.EmptyTypes);
            bool tcp = Str(input, "transport") == "tcp";
            if (tcp)
            {
                socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                socket.Bind(new IPEndPoint(IPAddress.Parse(Str(input, "interface_ip")), 0)); socket.NoDelay = true; socket.SendTimeout = 3000; socket.ReceiveTimeout = 3000;
                ft.GetField("comm_method").SetValue(form, 2); ft.GetField("newclient").SetValue(form, socket);
                string error = null; var done = new ManualResetEvent(false);
                Thread connector = new Thread(delegate() { try { error = (string)Call(commType, null, "ConnectSocket", new Type[] { typeof(IPEndPoint), typeof(Socket) }, new IPEndPoint(IPAddress.Parse(Str(input, "host")), 1000), socket); } catch (Exception e) { error = e.Message; } finally { done.Set(); } });
                connector.IsBackground = true; connector.Start(); Require(done.WaitOne(3000) && String.IsNullOrEmpty(error) && socket.Connected, "TCP connection failed: " + error);
            }
            else
            {
                serial = (SerialPort)ft.GetField("serialPort1").GetValue(form); serial.PortName = Str(input, "port"); serial.BaudRate = 460800; serial.DataBits = 8;
                serial.Parity = Parity.None; serial.StopBits = StopBits.One; serial.Handshake = Handshake.None; serial.ReadTimeout = 3000; serial.WriteTimeout = 3000;
                ft.GetField("comm_method").SetValue(form, 1); Call(commType, comm, "open", Type.EmptyTypes); Require(serial.IsOpen, "Serial open failed.");
            }
            var identity = inspectSerial ? ProtocolIdentity(tcp, true) : BoundIdentity(tcp, input);
            if (inspectSerial)
            {
                CheckExpectedIdentity(input, identity);
                object inspected;
                if (operation == "inspect_identity") inspected = new { handshake_verified = true };
                else if (operation == "inspect_catalog") inspected = DiscoverCatalog(input);
                else
                {
                    object[] requested = ((ArrayList)input["parameters"]).ToArray();
                    Require(requested.Length > 0 && requested.Length <= 20, "Inspect 1-20 parameters.");
                    var parameters = new List<object>();
                    foreach (Dictionary<string, object> a in requested)
                    {
                        int g = Int(a, "group"), o = Int(a, "offset");
                        Require(g >= 0x20 && g <= 0xEF && o >= 0 && o < 100, "Invalid inspected parameter address.");
                        var d = Definition((byte)g, (byte)o, false);
                        Require(Int(d, "access") != 3 && Convert.ToUInt32(d["attribute_raw"]) == Convert.ToUInt32(a["expected_attribute"]) && Str(d, "name") == Str(a, "expected_name"), "Parameter definition changed during serial inspection.");
                        d["code"] = Str(a, "code"); parameters.Add(d);
                    }
                    inspected = new { parameters = parameters };
                }
                var afterInspection = ProtocolIdentity(false, true);
                foreach (string key in identity.Keys) Require(Convert.ToString(identity[key]) == Convert.ToString(afterInspection[key]), "Device handshake changed during serial inspection; result discarded.");
                Require(!writeAttempted, "Serial inspection must never write device commands.");
                Emit(new { status = "passed", operation = operation, port = Str(input, "port"), identity = identity, result = inspected,
                    original_sha256 = VendorSha, device_write_commands_sent = false, frames = frames, connection_closed_after_operation = true,
                    physical_unique_identity_verified = false, identity_scope = "A/A1 platform and parameter revision; no product-specific MAC/UID layout assumed" });
                return 0;
            }
            Emit(Operate(input, identity, tcp));
            return 0;
        }
        catch (Exception e)
        { while (e is TargetInvocationException && e.InnerException != null) e = e.InnerException; Emit(new { status = "failed", error = e.ToString(), device_write_commands_sent = writeAttempted, frames = frames }); return 1; }
        finally { if (serial != null) serial.Dispose(); if (socket != null) socket.Dispose(); if (ownsLock) deviceLock.ReleaseMutex(); if (deviceLock != null) deviceLock.Dispose();
            commandReaderStopped = true; if (commandReader != null) commandReader.Join(1000);
            Stopwatch drain = Stopwatch.StartNew(); while (pendingCommandResults > 0 && drain.ElapsedMilliseconds < 3000) Thread.Sleep(10);
            if (logQueue != null) { logQueue.CompleteAdding(); logThread.Join(3000); }
            if (runJournal != null) runJournal.Dispose(); watchdog.Dispose(); }
    }
}
