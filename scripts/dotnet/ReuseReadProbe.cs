// Bounded read-only experiment using the original application's own classes.
using System;
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
using System.Web.Script.Serialization;

internal static class ReuseReadProbe
{
    private static readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private static Assembly original;
    private static Type communicationType;
    private static object communication;
    private static SerialPort serial;
    private static Socket network;
    private static readonly List<object> observations = new List<object>();

    private static void Emit(object data) { Console.WriteLine(json.Serialize(data)); Console.Out.Flush(); }
    private static Type OriginalType(string name) { return original.GetType("BonengDrivesoft." + name, true); }
    private static object Call(Type type, object instance, string name, Type[] signature, params object[] values)
    { return type.GetMethod(name, signature).Invoke(instance, values); }
    private static ushort UInt16(byte[] bytes, int offset)
    {
        return (ushort)Call(OriginalType("util.ByteUtil"), null, "toUInt16", new Type[] { typeof(byte[]) }, new byte[] { bytes[offset], bytes[offset + 1] });
    }

    private static byte[] Request(object entity, int responseLength)
    {
        byte[] frame = (byte[])entity.GetType().GetField("read_inquire").GetValue(entity);
        ushort crc = (ushort)Call(communicationType, null, "CRC16", new Type[] { typeof(byte[]), typeof(int), typeof(int) }, frame, 0, frame.Length - 2);
        frame[frame.Length - 2] = (byte)(crc >> 8);
        frame[frame.Length - 1] = (byte)crc;
        Emit(new { phase = "read_request", request_hex = BitConverter.ToString(frame) });
        bool sent = (bool)Call(communicationType, communication, "send_msg", new Type[] { typeof(byte[]) }, frame);
        if (!sent) throw new IOException("The original send_msg returned false.");
        // Wait for arrival before using the original fixed-length receive routine.
        Stopwatch wait = Stopwatch.StartNew();
        while ((network == null ? serial.BytesToRead : network.Available) < responseLength && wait.ElapsedMilliseconds < 2000) Thread.Sleep(2);
        if (network != null && network.Available < responseLength) throw new IOException("Incomplete TCP response before receive deadline.");
        byte[] response = (byte[])Call(communicationType, communication, "rcv_msg", new Type[] { typeof(int), entity.GetType() }, responseLength, entity);
        if (response == null) throw new IOException("The original receive routine returned no CRC-valid response.");
        if (response.Length != responseLength || response[0] != frame[0] || response[1] != frame[1]) throw new IOException("Response frame does not match the read request.");
        Emit(new { phase = "read_response", response_hex = BitConverter.ToString(response), original_crc_checked = true });
        return response;
    }

    private static object ReadParameter(byte group, byte offset, string code, string label)
    {
        Type entityType = OriginalType("communication.EntityModbus");
        Type actionType = OriginalType("communication.IAction");
        ConstructorInfo constructor = entityType.GetConstructor(new Type[] { typeof(ushort), typeof(byte), typeof(byte), typeof(byte), typeof(byte), typeof(int), actionType, typeof(int) });
        object entity = constructor.Invoke(new object[] { (ushort)0x55CC, (byte)0xFB, group, offset, (byte)1, 6, null, 0 });
        byte[] response = Request(entity, 11);
        if (response[2] != 0xFB || response[3] != group || response[4] != offset || response[5] != 1) throw new IOException("Parameter response identity/count mismatch.");
        return new { code = code, label = label, raw_value = UInt16(response, 6), decimal_attribute_raw = response[8], response_hex = BitConverter.ToString(response) };
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Timer watchdog = new Timer(delegate { Emit(new { status = "failed", error = "Read-only probe exceeded 45 seconds." }); Environment.Exit(2); }, null, 45000, Timeout.Infinite);
        try
        {
            if (args.Length < 2) throw new ArgumentException("Usage: ReuseReadProbe original-exe port [report-json] OR original-exe --tcp ipv4 port expected-mac local-ipv4 [report-json]");
            string path = Path.GetFullPath(args[0]);
            bool tcp = args[1] == "--tcp";
            bool identifyOnly = !tcp && args.Length >= 3 && args[2] == "--identify-only";
            if (tcp && args.Length < 6) throw new ArgumentException("TCP requires an IPv4 endpoint, expected MAC and local interface IPv4.");
            string port = tcp ? null : args[1];
            string host = tcp ? args[2] : null;
            int tcpPort = tcp ? Int32.Parse(args[3]) : 0;
            string expectedMac = tcp ? args[4].ToUpperInvariant() : null;
            string digest;
            using (SHA256 algorithm = SHA256.Create()) using (FileStream stream = File.OpenRead(path)) digest = BitConverter.ToString(algorithm.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            if (digest != "05b9be5e2e2a2cb74037c32451f89b0117dfa80983e4ff11b7b7c67912553f47")
                throw new InvalidOperationException("This probe has only been validated with the inspected Drivesoft V1.28 executable. SHA256: " + digest);
            if (!tcp) Emit(new { phase = "ports", available = SerialPort.GetPortNames(), requested = port });
            original = Assembly.LoadFrom(path);
            communicationType = OriginalType("communication.CCommModbus");
            byte[] test = Encoding.ASCII.GetBytes("123456789");
            ushort crc = (ushort)Call(communicationType, null, "CRC16", new Type[] { typeof(byte[]), typeof(int), typeof(int) }, test, 0, test.Length);
            if (crc != 0x374B) throw new InvalidOperationException("Original CRC API byte order differs from inspected implementation: " + crc.ToString("X4"));
            Emit(new { phase = "original_crc_verified", crc_api_result = crc.ToString("X4") });
            Emit(new { phase = "initializing_original_context" });
            Type formType = OriginalType("form.Form2");
            object form = Call(formType, null, "getInstance", Type.EmptyTypes);
            serial = (SerialPort)formType.GetField("serialPort1").GetValue(form);
            if (serial.IsOpen) throw new InvalidOperationException("Unexpected already-open serial port in isolated process.");
            if (!tcp) serial.PortName = port;
            serial.BaudRate = 460800;
            serial.DataBits = 8;
            serial.StopBits = StopBits.One;
            serial.Parity = Parity.None;
            serial.Handshake = Handshake.None;
            serial.ReadTimeout = 1000;
            serial.WriteTimeout = 1000;
            formType.GetField("Control_comm").SetValue(form, 1); // Original receive loop then waits rather than spinning.
            formType.GetField("Circle_comm").SetValue(form, 80);
            communication = Call(communicationType, null, "getInstance", Type.EmptyTypes);
            Emit(new { phase = "original_context_initialized", communication_background_tasks_started = false });
            if (tcp)
            {
                IPAddress target = IPAddress.Parse(host), local = IPAddress.Parse(args[5]);
                if (target.AddressFamily != AddressFamily.InterNetwork || local.AddressFamily != AddressFamily.InterNetwork || tcpPort != 1000) throw new ArgumentException("Only discovered IPv4 devices on the vendor TCP port 1000 are supported.");
                network = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
                network.Bind(new IPEndPoint(local, 0));
                network.SendTimeout = 2000; network.ReceiveTimeout = 2000; network.NoDelay = true;
                formType.GetField("comm_method").SetValue(form, 2);
                formType.GetField("newclient").SetValue(form, network);
                MethodInfo connect = communicationType.GetMethod("ConnectSocket", BindingFlags.NonPublic | BindingFlags.Static);
                string connectError = null;
                ManualResetEvent connected = new ManualResetEvent(false);
                Thread connector = new Thread(delegate() {
                    try { connectError = (string)connect.Invoke(null, new object[] { new IPEndPoint(target, tcpPort), network }); }
                    catch (Exception e) { connectError = e.Message; }
                    finally { connected.Set(); }
                });
                connector.IsBackground = true; connector.Start();
                if (!connected.WaitOne(3000)) { network.Close(); throw new IOException("Original TCP connection exceeded 3 seconds."); }
                connected.Dispose();
                if (!String.IsNullOrEmpty(connectError) || !network.Connected) throw new IOException("Original TCP connection failed: " + connectError);
                Emit(new { phase = "tcp_open", host = host, tcp_port = tcpPort, local_ip = args[5] });
            }
            else
            {
                Call(communicationType, communication, "open", Type.EmptyTypes);
                if (!serial.IsOpen) throw new IOException("The original open() did not open " + port + "; it may be unavailable or occupied.");
                Emit(new { phase = "port_open", port = port, baud_rate = serial.BaudRate });
            }
            Type entityType = OriginalType("communication.EntityModbus");
            Type actionType = OriginalType("communication.IAction");
            object handshake = entityType.GetConstructor(new Type[] { typeof(ushort), typeof(ushort), actionType, typeof(int) }).Invoke(new object[] { (ushort)0x55AA, (ushort)0xA5A5, null, 0 });
            byte[] version = Request(handshake, 8);
            if (version[2] != 0xA5 || version[3] != 0xA5) throw new IOException("Version handshake fixed code mismatch.");
            observations.Add(new { parameter_version_raw = UInt16(version, 4), response_hex = BitConverter.ToString(version) });
            object platformRequest = entityType.GetConstructor(new Type[] { typeof(ushort), typeof(byte), actionType, typeof(int) })
                    .Invoke(new object[] { (ushort)0x55AA, (byte)0xFA, null, 0 });
            byte[] identity = Request(platformRequest, 10);
            if (identity[2] != 0xFA || identity[3] != 2) throw new IOException("Platform handshake mismatch.");
            if (identifyOnly)
            {
                Emit(new { status = "passed", operation = "identify_serial", port = port, transport = "serial", baud_rate = 460800,
                    original_sha256 = digest, parameter_version_raw = UInt16(version, 4), platform_type = UInt16(identity, 4),
                    communication_type = UInt16(identity, 6), platform_response_hex = BitConverter.ToString(identity),
                    device_write_commands_sent = false, parameter_reads_sent = false, identity_layout_used = "common_A_A1_only" });
                return 0;
            }
            if (UInt16(identity, 4) != 9 || UInt16(identity, 6) != (tcp ? 3 : 0))
                throw new InvalidOperationException("This legacy MAC probe requires AN platform 9; use inspect_serial_device for this product.");
            observations.Add(ReadParameter(0x22, 0, "A02.00", "CM-ARM software version"));
            observations.Add(ReadParameter(0x20, 0, "A00.00", "State machine"));
            ushort[] macWords = new ushort[3];
            for (byte index = 0; index < 3; index++)
            {
                object observation = ReadParameter(0x45, (byte)(19 + index), "C05." + (19 + index), "Actual MAC address word " + (index + 1));
                observations.Add(observation);
                macWords[index] = (ushort)observation.GetType().GetProperty("raw_value").GetValue(observation, null);
            }
            byte[] octets = new byte[] { (byte)macWords[0], (byte)(macWords[0] >> 8), (byte)macWords[1], (byte)(macWords[1] >> 8), (byte)macWords[2], (byte)(macWords[2] >> 8) };
            string actualMac = BitConverter.ToString(octets).Replace('-', ':');
            if (tcp && actualMac != expectedMac) throw new IOException("Device MAC " + actualMac + " differs from selected scan MAC " + expectedMac + ".");
            object report = new { status = "passed", checked_at = DateTime.UtcNow.ToString("o"), original_executable = path, original_sha256 = digest, transport = tcp ? "tcp" : "serial", host = host, tcp_port = tcpPort, expected_mac = expectedMac, port = port, baud_rate = tcp ? 0 : 460800, data_bits = 8, stop_bits = 1, parity = "None", implementation = "original EntityModbus / ByteUtil / CCommModbus CRC16, open or ConnectSocket, send_msg, rcv_msg", device_write_commands_sent = false, observations = observations };
            string reportPath = tcp ? (args.Length >= 7 ? args[6] : null) : (args.Length >= 3 ? args[2] : null);
            if (!String.IsNullOrEmpty(reportPath) && reportPath != "-") File.WriteAllText(reportPath, json.Serialize(report), new UTF8Encoding(false));
            Emit(report);
            return 0;
        }
        catch (Exception error)
        {
            while (error is TargetInvocationException && error.InnerException != null) error = error.InnerException;
            Emit(new { status = "failed", error_type = error.GetType().FullName, error = error.Message, detail = error.ToString(), observations = observations });
            return 1;
        }
        finally
        {
            if (serial != null) serial.Dispose();
            if (network != null) network.Dispose();
            watchdog.Dispose();
        }
    }
}
