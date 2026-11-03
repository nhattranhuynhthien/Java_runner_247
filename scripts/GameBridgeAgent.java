import java.io.*;
import java.net.*;
import java.lang.reflect.*;
import java.lang.instrument.Instrumentation;
import java.text.Normalizer;
import java.util.*;

public class GameBridgeAgent {
    private static Instrumentation instrumentation;
    private static int serverPort = 28888;
    private static volatile boolean running = true;

    // Keep track of background farm task
    private static Thread farmThread = null;

    public static void premain(String agentArgs, Instrumentation inst) {
        instrumentation = inst;
        System.out.println("[BRIDGE] GameBridgeAgent premain loaded!");
        startHttpServer();
    }

    public static void agentmain(String agentArgs, Instrumentation inst) {
        instrumentation = inst;
        System.out.println("[BRIDGE] GameBridgeAgent agentmain loaded!");
        startHttpServer();
    }

    private static volatile ClassLoader cachedClassLoader = null;

    private static ClassLoader findMidletClassLoader() {
        if (cachedClassLoader != null) return cachedClassLoader;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            ClassLoader cl = t.getContextClassLoader();
            if (cl != null && cl.getClass().getName().contains("MIDletClassLoader")) {
                cachedClassLoader = cl;
                return cl;
            }
        }
        if (instrumentation != null) {
            for (Class<?> c : instrumentation.getAllLoadedClasses()) {
                if (c.getName().startsWith("nro.")) {
                    ClassLoader cl = c.getClassLoader();
                    if (cl != null) {
                        cachedClassLoader = cl;
                        return cl;
                    }
                }
            }
        }
        return null;
    }

    private static Class<?> getGameClass(String name) {
        ClassLoader cl = findMidletClassLoader();
        if (cl != null) {
            try {
                return cl.loadClass(name);
            } catch (Throwable ignored) {}
        }
        if (instrumentation != null) {
            for (Class<?> c : instrumentation.getAllLoadedClasses()) {
                if (c.getName().equals(name)) return c;
            }
        }
        try {
            return Class.forName(name);
        } catch (Throwable ignored) {}
        return null;
    }

    // Call Xmap travel to mapId
    public static boolean travelToMap(int mapId) {
        try {
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                Method bMeth = cls_f.getMethod("b", int.class);
                bMeth.invoke(null, mapId);
                System.out.println("[BRIDGE] Travel to map " + mapId + " triggered via nro.f.b(" + mapId + ")");
                return true;
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] travelToMap error: " + t);
        }
        return false;
    }

    // Stop Xmap
    public static boolean stopXmap() {
        try {
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                Method mMeth = cls_f.getMethod("m");
                mMeth.invoke(null);
                System.out.println("[BRIDGE] Stop Xmap triggered via nro.f.m()");
                return true;
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] stopXmap error: " + t);
        }
        return false;
    }

    // Execute a supported mod action directly without opening the game's chat UI.
    public static boolean executeModAction(String cmd) {
        if (cmd == null || cmd.trim().isEmpty()) return false;
        cmd = cmd.trim();
        boolean handled = false;
        try {
            // Special handling for the Xmap menu.
            if ("xmap".equalsIgnoreCase(cmd)) {
                Class<?> cls_f = getGameClass("nro.f");
                if (cls_f != null) {
                    Field aField = cls_f.getField("a");
                    Object inst_f = aField.get(null);
                    if (inst_f != null) {
                        Method nMeth = cls_f.getMethod("n");
                        nMeth.invoke(inst_f);
                        System.out.println("[BRIDGE] Opened Xmap menu via nro.f.a.n()");
                        handled = true;
                    }
                }
                if (handled) return true;
            }

            // Invoke the mod command handler directly.
            Class<?> clsF = getGameClass("nro.F");
            if (clsF != null) {
                Method aMeth = clsF.getMethod("a");
                Object instF = aMeth.invoke(null);
                if (instF != null) {
                    Method dMeth = clsF.getMethod("d", String.class);
                    Object resD = dMeth.invoke(instF, cmd);
                    if (Boolean.TRUE.equals(resD)) {
                        handled = true;
                    } else {
                        // Also try parametrized actions such as k5 and s20.
                        try {
                            Method eMeth = clsF.getMethod("e", String.class);
                            Object resE = eMeth.invoke(instF, cmd);
                            if (Boolean.TRUE.equals(resE)) handled = true;
                        } catch (Throwable ignored) {}
                    }
                    System.out.println("[BRIDGE] Executed nro.F cmd [" + cmd + "] -> " + handled);
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] executeModAction error for [" + cmd + "]: " + t);
        }
        return handled;
    }

    // Legacy custom-command route: direct mod action first, then game chat pipeline.
    public static boolean executeChatCmd(String cmd) {
        if (executeModAction(cmd)) return true;
        try {
            Class<?> clsAm = getGameClass("nro.am");
            if (clsAm != null) {
                Method aMeth = clsAm.getMethod("a");
                Object instAm = aMeth.invoke(null);
                if (instAm != null) {
                    Method pMeth = clsAm.getMethod("p", String.class);
                    pMeth.invoke(instAm, cmd.trim());
                    System.out.println("[BRIDGE] Executed via nro.am.a().p [" + cmd + "]");
                    return true;
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] executeChatCmd error for [" + cmd + "]: " + t);
        }
        return false;
    }

    // Hùng Hero's auto-farm needs the actual punch skill added to its skill list.
    // Resolve its 1-based skill number from the active character's own skill array.
    public static int configurePunchSkill() {
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCt = getGameClass("nro.ct");
            if (clsCr == null || clsCt == null) return -1;

            boolean alternateSkillSet = clsCr.getField("cS").getBoolean(null);
            Object array = clsCt.getField(alternateSkillSet ? "c" : "d").get(null);
            if (!(array instanceof Object[])) return -1;

            Object[] skills = (Object[]) array;
            for (int i = 0; i < skills.length; i++) {
                Object skill = skills[i];
                if (skill == null) continue;
                Object template = skill.getClass().getField("a").get(skill);
                if (template == null) continue;
                Object rawName = template.getClass().getField("b").get(template);
                if (!(rawName instanceof String)) continue;

                String normalized = Normalizer.normalize((String) rawName, Normalizer.Form.NFD)
                    .replaceAll("\\p{M}+", "")
                    .replace('đ', 'd').replace('Đ', 'D')
                    .trim().toLowerCase(Locale.ROOT);
                if (normalized.equals("dam") || normalized.startsWith("dam ") || normalized.startsWith("dam-")) {
                    int skillNumber = i + 1;
                    if (executeModAction("skill" + skillNumber)) {
                        System.out.println("[BRIDGE] Added Hùng Hero punch skill [" + rawName + "] (skill" + skillNumber + ") to auto combat.");
                        return skillNumber;
                    }
                    return -1;
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] configurePunchSkill error: " + t);
        }
        return -1;
    }

    // Get current game status
    public static Map<String, Object> getStatus() {
        Map<String, Object> status = new LinkedHashMap<String, Object>();
        status.put("bridge_online", true);

        try {
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                Field fE = cls_f.getField("e");
                Field fL = cls_f.getField("l");
                status.put("xmap_running", fE.getBoolean(null));
                status.put("target_map", fL.getInt(null));
            }
        } catch (Throwable ignored) {}

        try {
            Class<?> cls_av = getGameClass("nro.av");
            if (cls_av != null) {
                Field fH = cls_av.getField("h");
                Field fG = cls_av.getField("g");
                int currentMapId = fH.getInt(null);
                String[] mapNames = (String[]) fG.get(null);
                String currentMapName = (mapNames != null && currentMapId >= 0 && currentMapId < mapNames.length) ? mapNames[currentMapId] : "Unknown";
                status.put("map_id", currentMapId);
                status.put("map_name", currentMapName);
            }
        } catch (Throwable ignored) {}

        try {
            Class<?> cls_cd = getGameClass("nro.cd");
            if (cls_cd != null) {
                Method bMeth = cls_cd.getMethod("b");
                Object charObj = bMeth.invoke(null);
                if (charObj != null) {
                    Field fZone = charObj.getClass().getField("hH");
                    status.put("zone", fZone.getInt(charObj));
                }
            }
        } catch (Throwable ignored) {}

        return status;
    }

    private static void startHttpServer() {
        Thread serverThread = new Thread(new Runnable() {
            public void run() {
                try {
                    ServerSocket serverSocket = new ServerSocket(serverPort, 50, InetAddress.getByName("127.0.0.1"));
                    System.out.println("[BRIDGE] HTTP Bridge server listening on 127.0.0.1:" + serverPort);

                    while (running) {
                        final Socket socket = serverSocket.accept();
                        new Thread(new Runnable() {
                            public void run() {
                                handleClient(socket);
                            }
                        }).start();
                    }
                } catch (Throwable t) {
                    System.out.println("[BRIDGE] Server socket error: " + t);
                }
            }
        }, "NRO-Bridge-Server");
        serverThread.setDaemon(true);
        serverThread.start();
    }

    private static void handleClient(Socket socket) {
        try {
            socket.setSoTimeout(10000);
            BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream(), "UTF-8"));
            OutputStream out = socket.getOutputStream();

            String reqLine = in.readLine();
            if (reqLine == null) {
                socket.close();
                return;
            }

            String[] reqParts = reqLine.split(" ");
            String method = reqParts.length > 0 ? reqParts[0] : "GET";
            String fullPath = reqParts.length > 1 ? reqParts[1] : "/";

            // Read headers to get content length if POST
            int contentLength = 0;
            String line;
            while ((line = in.readLine()) != null && !line.isEmpty()) {
                if (line.toLowerCase().startsWith("content-length:")) {
                    try {
                        contentLength = Integer.parseInt(line.substring(15).trim());
                    } catch (Throwable ignored) {}
                }
            }

            String body = "";
            if (contentLength > 0) {
                char[] buf = new char[contentLength];
                int read = 0;
                while (read < contentLength) {
                    int r = in.read(buf, read, contentLength - read);
                    if (r < 0) break;
                    read += r;
                }
                body = new String(buf, 0, read);
            }

            String path = fullPath;
            String query = "";
            int qIdx = fullPath.indexOf("?");
            if (qIdx != -1) {
                path = fullPath.substring(0, qIdx);
                query = fullPath.substring(qIdx + 1);
            }

            Map<String, String> params = parseQuery(query);
            if (!body.isEmpty() && body.contains("=")) {
                params.putAll(parseQuery(body));
            }

            String jsonResp = "{}";
            int statusCode = 200;

            if (path.equals("/api/game/status")) {
                jsonResp = toJson(getStatus());
            } else if (path.equals("/api/game/xmap")) {
                String mapStr = params.get("map");
                if (mapStr != null) {
                    int mapId = Integer.parseInt(mapStr);
                    boolean ok = travelToMap(mapId);
                    jsonResp = "{\"status\": \"" + (ok ? "ok" : "error") + "\", \"map\": " + mapId + "}";
                } else {
                    statusCode = 400;
                    jsonResp = "{\"error\": \"Missing map parameter\"}";
                }
            } else if (path.equals("/api/game/stop_xmap")) {
                boolean ok = stopXmap();
                jsonResp = "{\"status\": \"" + (ok ? "ok" : "error") + "\"}";
            } else if (path.equals("/api/game/cmd")) {
                String cmd = params.get("cmd");
                if (cmd != null && !cmd.trim().isEmpty()) {
                    String[] subCmds = cmd.split("[,;]");
                    for (String sc : subCmds) {
                        sc = sc.trim();
                        if (!sc.isEmpty()) {
                            executeChatCmd(sc);
                            try { Thread.sleep(80); } catch (Throwable ignored) {}
                        }
                    }
                    jsonResp = "{\"status\": \"ok\", \"cmd\": \"" + cmd + "\"}";
                } else {
                    statusCode = 400;
                    jsonResp = "{\"error\": \"Missing cmd parameter\"}";
                }
            } else if (path.equals("/api/game/mod_action")) {
                String cmd = params.get("cmd");
                if (cmd != null && !cmd.trim().isEmpty()) {
                    String[] actions = cmd.split("[,;]");
                    String failedAction = null;
                    for (String action : actions) {
                        action = action.trim();
                        if (!action.isEmpty() && !executeModAction(action)) {
                            failedAction = action;
                            break;
                        }
                    }
                    if (failedAction == null) {
                        jsonResp = "{\"status\": \"ok\", \"cmd\": \"" + cmd + "\"}";
                    } else {
                        statusCode = 400;
                        jsonResp = "{\"status\": \"error\", \"error\": \"Unsupported mod action: " + failedAction + "\"}";
                    }
                } else {
                    statusCode = 400;
                    jsonResp = "{\"error\": \"Missing cmd parameter\"}";
                }
            } else if (path.equals("/api/game/configure_punch")) {
                int skillNumber = configurePunchSkill();
                if (skillNumber > 0) {
                    jsonResp = "{\"status\": \"ok\", \"skill\": " + skillNumber + "}";
                } else {
                    statusCode = 409;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Could not find or configure the punch skill for this character\"}";
                }
            } else if (path.equals("/api/game/auto_farm")) {
                // Batch auto farm sequence with smart map travel
                final String mapStr = params.get("map");
                final String zoneStr = params.get("zone");
                final String mode = params.get("mode") != null ? params.get("mode") : "all";

                if (farmThread != null && farmThread.isAlive()) {
                    farmThread.interrupt();
                }

                farmThread = new Thread(new Runnable() {
                    public void run() {
                        try {
                            Integer targetMap = null;
                            if (mapStr != null && !mapStr.equals("xmap") && !mapStr.equals("0")) {
                                try { targetMap = Integer.parseInt(mapStr); } catch (Throwable ignored) {}
                            }

                            // 1. If target map specified, check if already there
                            if (targetMap != null) {
                                Map<String, Object> curStat = getStatus();
                                Integer curMap = (Integer) curStat.get("map_id");
                                if (curMap == null || !curMap.equals(targetMap)) {
                                    System.out.println("[BRIDGE] AutoFarm: Traveling to map " + targetMap + "...");
                                    if (!travelToMap(targetMap)) {
                                        System.out.println("[BRIDGE] AutoFarm: Could not start travel to map " + targetMap + ".");
                                        return;
                                    }

                                    // Wait until arrived at target map (or max 60 seconds)
                                    long startWait = System.currentTimeMillis();
                                    boolean arrived = false;
                                    while (System.currentTimeMillis() - startWait < 60000) {
                                        Thread.sleep(1000);
                                        Map<String, Object> st = getStatus();
                                        Integer cm = (Integer) st.get("map_id");
                                        Boolean xmapRun = (Boolean) st.get("xmap_running");
                                        if (cm != null && cm.equals(targetMap) && (xmapRun == null || !xmapRun)) {
                                            System.out.println("[BRIDGE] AutoFarm: Arrived at target map " + targetMap + "!");
                                            arrived = true;
                                            break;
                                        }
                                    }
                                    if (!arrived) {
                                        System.out.println("[BRIDGE] AutoFarm: Timed out waiting for map " + targetMap + "; farm actions cancelled.");
                                        return;
                                    }
                                    // Settle after arrival
                                    Thread.sleep(1200);
                                }
                            }

                            // 2. Switch zone if specified
                            if (zoneStr != null && !zoneStr.trim().isEmpty()) {
                                try {
                                    int z = Integer.parseInt(zoneStr.trim());
                                    executeModAction("k" + z);
                                    Thread.sleep(1000);
                                } catch (Throwable ignored) {}
                            }

                            // 3. Activate auto farm mode: TDLT + TÀN SÁT COMBO
                            if ("ts".equalsIgnoreCase(mode)) {
                                executeModAction("ts");
                            } else if ("tdlt".equalsIgnoreCase(mode)) {
                                executeModAction("tdlt");
                            } else {
                                // Default COMBO: activate both tdlt AND ts
                                executeModAction("tdlt");
                                Thread.sleep(350);
                                executeModAction("ts");
                            }
                            Thread.sleep(300);

                            // 4. Activate support utilities
                            executeModAction("locdo");
                            Thread.sleep(250);
                            executeModAction("anhat");
                            Thread.sleep(250);
                            executeModAction("gb");
                            Thread.sleep(250);
                            executeModAction("s20");
                            Thread.sleep(250);
                            executeModAction("xdh");
                            Thread.sleep(250);
                            executeModAction("annguoi");

                            System.out.println("[BRIDGE] AutoFarm: Combo (TDLT + Tàn Sát + Lọc Đồ + Auto Nhặt + Goback + Siêu Mượt) activated successfully!");
                        } catch (InterruptedException ie) {
                            System.out.println("[BRIDGE] AutoFarm thread interrupted.");
                        } catch (Throwable t) {
                            System.out.println("[BRIDGE] AutoFarm error: " + t);
                        }
                    }
                }, "AutoFarm-Worker");
                farmThread.setDaemon(true);
                farmThread.start();

                jsonResp = "{\"status\": \"ok\", \"message\": \"Auto farm started\", \"map\": \"" + mapStr + "\", \"zone\": \"" + zoneStr + "\", \"mode\": \"" + mode + "\"}";
            } else if (path.equals("/api/game/stop_farm")) {
                if (farmThread != null && farmThread.isAlive()) {
                    farmThread.interrupt();
                }
                stopXmap();
                executeModAction("ts");
                try { Thread.sleep(100); } catch (Throwable ignored) {}
                executeModAction("tdlt");
                try { Thread.sleep(100); } catch (Throwable ignored) {}
                executeModAction("xdh");
                try { Thread.sleep(100); } catch (Throwable ignored) {}
                executeModAction("annguoi");
                jsonResp = "{\"status\": \"ok\", \"message\": \"Auto farm stopped & graphics restored\"}";
            } else {
                statusCode = 404;
                jsonResp = "{\"error\": \"Not found\"}";
            }

            byte[] respBytes = jsonResp.getBytes("UTF-8");
            String header = "HTTP/1.1 " + statusCode + " OK\r\n" +
                            "Content-Type: application/json; charset=UTF-8\r\n" +
                            "Content-Length: " + respBytes.length + "\r\n" +
                            "Access-Control-Allow-Origin: *\r\n" +
                            "Connection: close\r\n\r\n";
            out.write(header.getBytes("UTF-8"));
            out.write(respBytes);
            out.flush();
            socket.close();
        } catch (Throwable t) {
            try { socket.close(); } catch (Throwable ignored) {}
        }
    }

    private static Map<String, String> parseQuery(String q) {
        Map<String, String> m = new HashMap<String, String>();
        if (q == null || q.isEmpty()) return m;
        for (String pair : q.split("&")) {
            int idx = pair.indexOf("=");
            if (idx > 0) {
                try {
                    String k = URLDecoder.decode(pair.substring(0, idx), "UTF-8");
                    String v = URLDecoder.decode(pair.substring(idx + 1), "UTF-8");
                    m.put(k, v);
                } catch (Throwable ignored) {}
            }
        }
        return m;
    }

    private static String toJson(Map<String, Object> map) {
        StringBuilder sb = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) sb.append(",");
            first = false;
            sb.append("\"").append(e.getKey()).append("\":");
            Object v = e.getValue();
            if (v == null) sb.append("null");
            else if (v instanceof Number || v instanceof Boolean) sb.append(v);
            else sb.append("\"").append(v.toString().replace("\"", "\\\"")).append("\"");
        }
        sb.append("}");
        return sb.toString();
    }
}
