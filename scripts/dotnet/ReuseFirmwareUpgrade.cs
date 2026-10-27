using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Ports;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// A bounded, device/file-pinned wrapper around the inspected vendor updater.
// No vendor Main(), communication scheduler, or automatic motor commands run.
internal static class ReuseFirmwareUpgrade
{
    private const string UpdaterSha = "3cda061d737adee08e582f3f28f323e7a283312dc1a6b84534813a2d4a321334";
    private const string FirmwareSha = "cc039bf224607f71e1a8a2ab5358fa8e17c8cc4230ccb99a91640a9fa8301a07";
    private static Assembly vendor;
    private static object form, comm;
    private static Type formType, commType, entityType, actionType;
    private static SerialPort serial;
    private static Socket network;
    private static Mutex deviceLock;
    private static bool ownsDeviceLock;
    private static StreamWriter journal;
    private static readonly object logLock = new object();
    private static readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private static string stage = "validation";
    private static bool enteredUpgrade, flashVerified;

    private static void Log(object value)
    {
        lock (logLock)
        {
            string line = json.Serialize(new { at = DateTime.UtcNow.ToString("o"), data = value });
            try { Console.WriteLine(line); } catch (IOException) { }
            if (journal != null) { journal.WriteLine(line); journal.Flush(); }
        }
    }
    private static Type T(string name) { return vendor.GetType("Downloadconfig." + name, true); }
    private static object Get(string name) { return formType.GetField(name).GetValue(form); }
    private static void Set(string name, object value) { formType.GetField(name).SetValue(form, value); }
    private static object Invoke(Type type, object target, string name, Type[] signature, params object[] values)
    {
        MethodInfo method = type.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static, null, signature, null);
        if (method == null) throw new MissingMethodException(type.FullName, name);
        return method.Invoke(target, values);
    }
    private static string Sha(byte[] data)
    {
        using (SHA256 algorithm = SHA256.Create()) return BitConverter.ToString(algorithm.ComputeHash(data)).Replace("-", "").ToLowerInvariant();
    }
    private static void Require(bool condition, string reason) { if (!condition) throw new InvalidOperationException(reason); }
    private static void AddCrc(byte[] frame)
    {
        ushort crc = (ushort)Invoke(commType, null, "CRC16", new Type[] { typeof(byte[]), typeof(int), typeof(int) }, frame, 0, frame.Length - 2);
        frame[frame.Length - 2] = (byte)(crc >> 8); frame[frame.Length - 1] = (byte)crc;
    }
    private static byte[] Read(object entity, int size)
    {
        byte[] request = (byte[])entityType.GetField("read_inquire").GetValue(entity);
        AddCrc(request);
        Require((bool)Invoke(commType, comm, "send_msg", new Type[] { typeof(byte[]) }, request), "Vendor read send failed.");
        Stopwatch elapsed = Stopwatch.StartNew();
        while ((network == null ? serial.BytesToRead : network.Available) < size && elapsed.ElapsedMilliseconds < 2000) Thread.Sleep(2);
        if (network != null) Require(network.Available >= size, "Incomplete TCP response before receive deadline.");
        byte[] response = (byte[])Invoke(commType, comm, "rcv_msg", new Type[] { typeof(int), entityType }, size, entity);
        Require(response != null && response.Length == size && response[0] == request[0] && response[1] == request[1] && response[2] == request[2], "Missing or mismatched CRC-valid read response.");
        Log(new { phase = "read", request_hex = BitConverter.ToString(request), response_hex = BitConverter.ToString(response) });
        return response;
    }
    private static ushort Parameter(byte group, byte offset)
    {
        object entity = entityType.GetConstructor(new Type[] { typeof(Control), typeof(ushort), typeof(byte), typeof(byte), typeof(byte), typeof(byte), actionType, typeof(int) })
            .Invoke(new object[] { null, (ushort)0x55CC, (byte)0xFB, group, offset, (byte)1, null, 1 });
        byte[] response = Read(entity, 11);
        Require(response[3] == group && response[4] == offset && response[5] == 1, "Parameter response address/count mismatch.");
        return BitConverter.ToUInt16(response, 6);
    }
    private static int ReadParameterVersion()
    {
        byte[] request = new byte[] { 0xAA, 0x55, 0xA5, 0xA5, 0, 0 }; AddCrc(request);
        Require((bool)Invoke(commType, comm, "send_msg", new Type[] { typeof(byte[]) }, request), "Parameter-version read send failed.");
        byte[] response = new byte[8]; int position = 0; Stopwatch deadline = Stopwatch.StartNew();
        while (position < response.Length)
        {
            Require(deadline.ElapsedMilliseconds < 2000, "Parameter-version response timed out.");
            int available = network == null ? serial.BytesToRead : network.Available;
            if (available == 0) { Thread.Sleep(2); continue; }
            int count = network == null ? serial.Read(response, position, Math.Min(available, 8-position)) : network.Receive(response, position, Math.Min(available, 8-position), SocketFlags.None);
            Require(count > 0, "Parameter-version connection closed."); position += count;
        }
        Require((bool)Invoke(commType, comm, "check_crc", new Type[] { typeof(byte[]) }, response) && response[0] == 0xAA && response[1] == 0x55 && response[2] == 0xA5 && response[3] == 0xA5, "Parameter-version CRC/frame mismatch.");
        return BitConverter.ToUInt16(response, 4);
    }
    private static object Page(int page, int index)
    {
        return ((IList)((Hashtable)Get("comm_page"))[page])[index];
    }
    private static bool Action(object entity, string method)
    {
        object action = T("communication.EntityBase").GetField("action").GetValue(entity);
        return (bool)Invoke(action.GetType(), action, method, new Type[] { T("communication.EntityBase") }, entity);
    }
    private static Dictionary<string, object> Cm55Fingerprint(Dictionary<string, object> selection)
    {
        var actual = new Dictionary<string, object> {
            { "platform_type", 3 }, { "communication_type", 0 },
            { "board_number_raw", (int)Parameter(0x22, 10) }, { "platform_raw", (int)Parameter(0x22, 11) } };
        Require(Convert.ToInt32(actual["platform_raw"]) % 10000 == 301, "CM55 EC platform variant mismatch.");
        var expected = (Dictionary<string, object>)selection["expected_serial_identity"];
        foreach (string key in actual.Keys)
            Require(expected.ContainsKey(key) && Convert.ToString(expected[key]) == Convert.ToString(actual[key]), "CM55 serial fingerprint changed; no upgrade sent.");
        return actual;
    }
    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        System.Threading.Timer progress = null;
        try
        {
            Require(args.Length >= 5, "Usage: updater firmware port journal mode [--tcp host mac local-ip] [--contract build-json] [--serial-target selection-json sha binding-id]");
            string contractPath = null, selectionPath = null, selectionSha = null, bindingId = null, host = null, localIp = null, tcpMac = null, serialMac = null;
            for (int i = 5; i < args.Length; )
            {
                if (args[i] == "--contract" && contractPath == null && i + 1 < args.Length) { contractPath = args[i+1]; i += 2; }
                else if (args[i] == "--tcp" && host == null && i + 3 < args.Length) { host = args[i+1]; tcpMac = args[i+2]; localIp = args[i+3]; i += 4; }
                else if (args[i] == "--serial-target" && selectionPath == null && i + 3 < args.Length) { selectionPath = args[i+1]; selectionSha = args[i+2]; bindingId = args[i+3]; i += 4; }
                else if (args[i] == "--serial-mac" && serialMac == null && i + 1 < args.Length) { serialMac = args[i+1].ToUpperInvariant(); i += 2; }
                else throw new InvalidOperationException("Invalid or duplicate upgrade option.");
            }
            bool tcp = host != null, hasContract = contractPath != null, cm55 = false;
            string firmwareSha = FirmwareSha, expectedRelease = "6908"; int expectedSoftware = 910; int? expectedParameterVersion = null;
            if (hasContract)
            {
                var contract = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(contractPath, Encoding.UTF8));
                cm55 = Convert.ToString(contract["profile"]) == "cm55_ec_spi_612";
                expectedSoftware = cm55 ? 612 : 910;
                Require((cm55 || Convert.ToString(contract["profile"]) == "an_pn_spi_910") && Convert.ToInt32(contract["expected_software_raw"]) == expectedSoftware && Convert.ToInt32(contract["platform_type"]) == (cm55 ? 3 : 9), "Unsupported registered build profile.");
                firmwareSha = Convert.ToString(contract["firmware_sha256"]); expectedRelease = Convert.ToString(contract["expected_release_hex"]);
                Require(Regex.IsMatch(firmwareSha, "^[0-9a-f]{64}$") && Regex.IsMatch(expectedRelease, "^[0-9A-F]{4}$"), "Invalid build digest/release.");
                if (contract["expected_parameter_version_raw"] != null) expectedParameterVersion = Convert.ToInt32(contract["expected_parameter_version_raw"]);
            }
            Dictionary<string, object> selection = null, serialFingerprint = null;
            Require(cm55 == (selectionPath != null) && (!cm55 || !tcp), "CM55 images require a selected serial target; AN/CM55 profiles cannot be mixed.");
            if (cm55)
            {
                byte[] selectedBytes = File.ReadAllBytes(selectionPath);
                Require(Sha(selectedBytes) == selectionSha && Regex.IsMatch(bindingId, "^[0-9a-f]{32}$"), "Serial selection changed since preflight.");
                selection = json.Deserialize<Dictionary<string, object>>(Encoding.UTF8.GetString(selectedBytes));
                Require(Convert.ToString(selection["serial_binding_id"]) == bindingId && Convert.ToString(selection["device_profile"]) == "cm55_ec_612" && Convert.ToString(selection["port"]) == args[2] && Convert.ToString(selection["transport"]) == "serial" && selection["expected_mac"] == null, "Invalid CM55 serial selection.");
            }
            Require(serialMac == null || (!tcp && !cm55), "Serial MAC is only valid for the AN serial profile.");
            string expectedMac = cm55 ? null : tcp ? tcpMac.ToUpperInvariant() : serialMac;
            Require(cm55 || expectedMac != null, "AN serial upgrade requires an explicit --serial-mac target.");
            Require(cm55 || Regex.IsMatch(expectedMac, "^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$"), "TCP requires the selected device MAC.");
            deviceLock = new Mutex(false, "Local\\BonengFirmwareUpgrade-" + (tcp ? expectedMac.Replace(":", "") : "Serial-" + args[2]));
            try { ownsDeviceLock = deviceLock.WaitOne(0); }
            catch (AbandonedMutexException) { ownsDeviceLock = true; }
            Require(ownsDeviceLock, "Another firmware operation holds this device; no command was sent.");
            bool execute = args[4] == "--execute";
            bool verifyBoot = args[4] == "--verify-boot";
            Require(execute || verifyBoot || args[4] == "--preflight", "Unknown mode.");
            Require((tcp && args[2] == "TCP") || (!tcp && (cm55 ? Regex.IsMatch(args[2], "^COM[1-9][0-9]{0,3}$") : args[2] == "COM7")), "Choose a selected serial endpoint or TCP target.");
            journal = new StreamWriter(args[3], false, new UTF8Encoding(false));
            string updaterPath = Path.GetFullPath(args[0]);
            string firmwarePath = Path.GetFullPath(args[1]);
            byte[] image = File.ReadAllBytes(firmwarePath);
            Require(Sha(File.ReadAllBytes(updaterPath)) == UpdaterSha, "Updater hash differs from inspected vendor build.");
            Require(Sha(image) == firmwareSha, "Firmware hash differs from the registered or inspected image.");
            Require((cm55 ? image.Length >= 20 && image.Length <= 0x200000 : image.Length == 1966100) && BitConverter.ToUInt32(image, 0) == 0xEE3355BB && BitConverter.ToUInt32(image, 4) == image.Length - 4 && BitConverter.ToUInt32(image, 8) == 0x80000000 && BitConverter.ToUInt32(image, 12) == 0x80000000, "Unexpected SPI application header.");
            Log(new { phase = "files_validated", execute = execute, updater_sha256 = UpdaterSha, firmware_sha256 = firmwareSha, firmware = firmwarePath, bytes = image.Length, registered_build = hasContract });
            vendor = Assembly.LoadFrom(updaterPath);
            formType = T("Form1"); commType = T("communication.CCommModbus"); entityType = T("communication.EntityModbus"); actionType = T("communication.IAction");
            form = Invoke(formType, null, "getinstance", Type.EmptyTypes);
            // Create a hidden handle so the vendor F3 BeginInvoke has a target.
            IntPtr handle = ((Control)form).Handle;
            Set("Control_comm", 1); Set("Circle_comm", 200); Set("Control_type", cm55 ? 3 : 9); Set("comm_method", tcp ? 2 : 1); Set("BaudRate", 460800);
            Set("restart", false); Set("up_flag", 0); Set("complete_index", 0); Set("cheack_ok", 0);
            Set("binData", image);
            object f1 = Page(2, 0), f2 = Page(2, 1), f3 = Page(4, 0);
            object f1Action = T("communication.EntityBase").GetField("action").GetValue(f1);
            Require(f1Action.GetType().FullName == "Downloadconfig.action.Action_F1_CM25", "Wrong vendor transfer action.");
            byte[] beginTemplate = (byte[])entityType.GetField("write_inquire").GetValue(f2);
            Require(beginTemplate.Length == 10 && beginTemplate[0] == 0xFF && beginTemplate[1] == 0x55 && beginTemplate[2] == 0xFB && beginTemplate[7] == 1, "Wrong vendor AM3356 begin entity.");
            serial = (SerialPort)Get("serialPort1");
            if (!tcp) serial.PortName = args[2];
            serial.BaudRate = 460800; serial.DataBits = 8; serial.Parity = Parity.None; serial.StopBits = StopBits.One; serial.Handshake = Handshake.None;
            serial.ReadTimeout = 2000; serial.WriteTimeout = 2000;
            comm = Invoke(commType, null, "getInstance", Type.EmptyTypes);
            if (tcp)
            {
                IPAddress remote = IPAddress.Parse(host), local = IPAddress.Parse(localIp);
                Require(remote.AddressFamily == AddressFamily.InterNetwork && local.AddressFamily == AddressFamily.InterNetwork, "TCP upgrade requires IPv4.");
                network = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                network.Bind(new IPEndPoint(local, 0));
                network.SendTimeout = 2000; network.ReceiveTimeout = 2000; network.NoDelay = true;
                Set("newclient", network);
                string connectError = null;
                ManualResetEvent connected = new ManualResetEvent(false);
                Thread connector = new Thread(delegate() {
                    try { connectError = (string)Invoke(commType, null, "ConnectSocket", new Type[] { typeof(IPEndPoint), typeof(Socket) }, new IPEndPoint(remote, 1000), network); }
                    catch (Exception e) { connectError = e.Message; }
                    finally { connected.Set(); }
                });
                connector.IsBackground = true; connector.Start();
                if (!connected.WaitOne(3000)) { network.Close(); throw new IOException("Vendor TCP connection exceeded 3 seconds."); }
                connected.Dispose();
                Require(String.IsNullOrEmpty(connectError) && network.Connected, "Vendor TCP connection failed: " + connectError);
                Log(new { phase = "tcp_open", host = host, tcp_port = 1000, expected_mac = expectedMac, local_ip = localIp });
            }
            else
            {
                Invoke(commType, comm, "open", Type.EmptyTypes);
                Require(serial.IsOpen, "Could not open COM7.");
            }
            stage = "preflight";
            object a1 = entityType.GetConstructor(new Type[] { typeof(Control), typeof(ushort), typeof(byte), actionType, typeof(int) })
                .Invoke(new object[] { null, (ushort)0x55AA, (byte)0xFA, null, 1 });
            byte[] identity = Read(a1, 10);
            Require(identity[3] == 2 && BitConverter.ToUInt16(identity, 4) == (cm55 ? 3 : 9) && BitConverter.ToUInt16(identity, 6) == (tcp ? 3 : 0), "Target platform/transport mismatch.");
            string actualMac = null;
            if (cm55) serialFingerprint = Cm55Fingerprint(selection);
            else
            {
                ushort mac1 = Parameter(0x45, 19), mac2 = Parameter(0x45, 20), mac3 = Parameter(0x45, 21);
                actualMac = BitConverter.ToString(new byte[] { (byte)mac1, (byte)(mac1 >> 8), (byte)mac2, (byte)(mac2 >> 8), (byte)mac3, (byte)(mac3 >> 8) }).Replace('-', ':');
            }
            Require(actualMac == expectedMac, "Connected device MAC differs from the authorized target: " + actualMac);
            ushort software = Parameter(0x22, 0), release = Parameter(0x22, 8), platform = Parameter(0x22, 11), state = Parameter(0x20, 0);
            Require((cm55 ? platform % 10000 == 301 : platform / 100 == 9) && software == expectedSoftware, "Device platform/software changed from preflight.");
            ushort runStatus = Parameter(0xEC, 7);
            // Firmware respondF2 permits stop status (run bit clear), not only S1.
            if (!verifyBoot) Require((state == 1 || state == 4 || state == 5) && (runStatus & 4) == 0, "Upgrade requires stopped state 1/4/5 and a cleared run bit.");
            Log(new { phase = "preflight_passed", mac = actualMac, serial_binding_id = bindingId, serial_fingerprint = serialFingerprint, physical_unique_identity_verified = !cm55, transport = tcp ? "tcp" : "serial", software_raw = software, release_hex = release.ToString("X4"), platform_raw = platform, state_raw = state, run_status_word = runStatus, chunk_bytes = tcp ? 512 : 120, transfer_action = f1Action.GetType().FullName });
            if (verifyBoot)
            {
                byte[] flashResult = Read(f3, 6);
                Require(flashResult[2] == 0xFC && flashResult[3] == 0, "Upgrade result has not returned to its boot default; physical power cycle is not yet verified.");
                Require(release.ToString("X4") == expectedRelease && software == expectedSoftware, "Booted release/software differs from the requested build.");
                int parameterVersion = ReadParameterVersion();
                Require(!expectedParameterVersion.HasValue || parameterVersion == expectedParameterVersion.Value, "Booted parameter revision differs from the registered build.");
                Log(new { status = "boot_verified", firmware_sha256 = firmwareSha, software_raw = software, release_hex = release.ToString("X4"), parameter_version_raw = parameterVersion,
                    mac = actualMac, serial_binding_id = bindingId, serial_fingerprint = serialFingerprint, physical_unique_identity_verified = !cm55, state_raw = state, upgrade_status_reset_to_zero = true, device_write_commands_sent = false,
                    identity_strength = "version/parameter revision plus verified Flash receipt; not an executable runtime image hash" });
                return 0;
            }
            if (!execute) { Log(new { status = "preflight_passed", device_write_commands_sent = false }); return 0; }
            stage = "begin_upgrade";
            Log(new { phase = stage, method = "vendor registered F2 uptype=1" });
            enteredUpgrade = true;
            Require(Action(f2, "doWrite") && (int)Get("up_flag") == 1, "Vendor F2 did not confirm upgrade acceptance; no F1 payload sent.");
            Log(new { phase = "upgrade_accepted", flag = Get("up_flag") });
            stage = "transfer";
            ProgressBar bar = (ProgressBar)Get("progressBar1");
            progress = new System.Threading.Timer(delegate {
                try { Log(new { phase = "transfer_progress", packet = bar.Value, total = bar.Maximum, complete_flag = Get("complete_index") }); }
                catch (Exception e) { Log(new { phase = "progress_read_error", error = e.Message }); }
            }, null, 5000, 5000);
            bool sent = Action(f1, "doWrite");
            progress.Dispose(); progress = null;
            Log(new { phase = "transfer_finished", vendor_return = sent, complete_flag = Get("complete_index"), packet = bar.Value, total = bar.Maximum });
            Require(sent && (int)Get("complete_index") == 1, "Vendor transfer did not finish successfully; do not restart or retry writing automatically.");
            stage = "flash_verification";
            Stopwatch flashWait = Stopwatch.StartNew();
            do
            {
                Thread.Sleep(1000);
                bool reply = Action(f3, "doRead");
                Application.DoEvents();
                int flag = (int)Get("cheack_ok");
                Log(new { phase = stage, reply = reply, flag = flag, elapsed_seconds = flashWait.Elapsed.TotalSeconds });
                if (reply && flag == 1) { flashVerified = true; break; }
                Require(!reply || flag != 2, "Device reported Flash programming/readback failure.");
            } while (flashWait.Elapsed.TotalSeconds < 180);
            Require(flashVerified, "No confirmed flash result within 180 seconds; no restart sent.");
            Log(new { status = "flash_verified", device_flash_readback_passed = true, reboot_sent = false, firmware_sha256 = firmwareSha, mac = actualMac, serial_binding_id = bindingId, serial_fingerprint = serialFingerprint, physical_unique_identity_verified = !cm55 });
            return 0;
        }
        catch (Exception error)
        {
            while (error is TargetInvocationException && error.InnerException != null) error = error.InnerException;
            Log(new { status = "failed", stage = stage, upgrade_command_attempted = enteredUpgrade, flash_verified = flashVerified, error = error.ToString() });
            return 1;
        }
        finally
        {
            if (progress != null) progress.Dispose();
            if (serial != null) serial.Dispose();
            if (network != null) network.Dispose();
            if (form != null) ((IDisposable)form).Dispose();
            if (journal != null) journal.Dispose();
            if (ownsDeviceLock) deviceLock.ReleaseMutex();
            if (deviceLock != null) deviceLock.Dispose();
        }
    }
}
