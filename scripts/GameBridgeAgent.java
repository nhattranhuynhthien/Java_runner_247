import java.io.*;
import java.net.*;
import java.lang.reflect.*;
import java.lang.instrument.Instrumentation;
import java.text.Normalizer;
import java.util.*;
import java.awt.Component;
import java.awt.Container;
import java.awt.Graphics2D;
import java.awt.Window;
import java.awt.image.BufferedImage;
import javax.imageio.ImageIO;

public class GameBridgeAgent {
    private static Instrumentation instrumentation;
    private static int serverPort = 28888;
    private static volatile boolean running = true;

    // Keep track of background farm task & persistent 24/7 state
    private static Thread farmThread = null;
    private static volatile boolean autoFarmActive = false;
    private static volatile int autoFarmMap = 0;
    private static volatile int autoFarmZone = -1;
    private static volatile String autoFarmMode = "all";
    private static volatile long lastWatchdogTravelMs = 0L;

    private static File getStateFile() {
        File dataDir = new File("/data");
        if (dataDir.exists() && dataDir.isDirectory() && dataDir.canWrite()) {
            return new File(dataDir, "autofarm_state.json");
        }
        String home = System.getProperty("user.home", ".");
        return new File(home, "autofarm_state.json");
    }

    public static synchronized void saveAutoFarmState() {
        try {
            File f = getStateFile();
            String json = "{\"active\":" + autoFarmActive +
                          ",\"map\":" + autoFarmMap +
                          ",\"zone\":" + autoFarmZone +
                          ",\"mode\":\"" + (autoFarmMode != null ? autoFarmMode : "all") + "\"" +
                          ",\"updated_at\":" + System.currentTimeMillis() + "}";
            FileOutputStream fos = new FileOutputStream(f);
            fos.write(json.getBytes("UTF-8"));
            fos.close();
        } catch (Throwable t) {
            System.out.println("[BRIDGE] saveAutoFarmState error: " + t);
        }
    }

    public static synchronized void loadAutoFarmState() {
        try {
            File f = getStateFile();
            if (!f.exists()) return;
            BufferedReader br = new BufferedReader(new InputStreamReader(new FileInputStream(f), "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) sb.append(line);
            br.close();
            String s = sb.toString();
            autoFarmActive = s.contains("\"active\":true") || s.contains("\"active\": true");
            autoFarmMap = extractIntJson(s, "map", 0);
            autoFarmZone = extractIntJson(s, "zone", -1);
            String m = extractStrJson(s, "mode", "all");
            autoFarmMode = (m != null && !m.isEmpty()) ? m : "all";
            System.out.println("[BRIDGE] Loaded persisted AutoFarm state: active=" + autoFarmActive +
                               ", map=" + autoFarmMap + ", zone=" + autoFarmZone + ", mode=" + autoFarmMode);
        } catch (Throwable t) {
            System.out.println("[BRIDGE] loadAutoFarmState error: " + t);
        }
    }

    private static int extractIntJson(String json, String key, int defVal) {
        try {
            String pat = "\"" + key + "\":";
            int idx = json.indexOf(pat);
            if (idx < 0) return defVal;
            int start = idx + pat.length();
            while (start < json.length() && Character.isWhitespace(json.charAt(start))) start++;
            int end = start;
            if (end < json.length() && (json.charAt(end) == '-' || Character.isDigit(json.charAt(end)))) {
                end++;
                while (end < json.length() && Character.isDigit(json.charAt(end))) end++;
                return Integer.parseInt(json.substring(start, end));
            }
        } catch (Throwable ignored) {}
        return defVal;
    }

    private static String extractStrJson(String json, String key, String defVal) {
        try {
            String pat = "\"" + key + "\":\"";
            int idx = json.indexOf(pat);
            if (idx < 0) return defVal;
            int start = idx + pat.length();
            int end = json.indexOf("\"", start);
            if (end > start) return json.substring(start, end);
        } catch (Throwable ignored) {}
        return defVal;
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        instrumentation = inst;
        System.out.println("[BRIDGE] GameBridgeAgent premain loaded!");
        loadAutoFarmState();
        startHttpServer();
        startWatchdogThread();
    }

    public static void agentmain(String agentArgs, Instrumentation inst) {
        instrumentation = inst;
        System.out.println("[BRIDGE] GameBridgeAgent agentmain loaded!");
        loadAutoFarmState();
        startHttpServer();
        startWatchdogThread();
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

    private static String[] resolveModFlagTarget(String cmd) {
        if (cmd == null) return null;
        String c = cmd.trim().toLowerCase(Locale.ROOT);
        if ("ts".equals(c)) return new String[]{"nro.j", "n"};
        if ("tdlt".equals(c)) return new String[]{"nro.p", "z"};
        if ("gb".equals(c)) return new String[]{"nro.A", "J"};
        if ("anhat".equals(c)) return new String[]{"nro.i", "l"};
        if ("locdo".equals(c)) return new String[]{"nro.F", "O"};
        if ("xdh".equals(c)) return new String[]{"nro.E", "N"};
        if ("annguoi".equals(c)) return new String[]{"nro.N", "ag"};
        if ("anquai".equals(c)) return new String[]{"nro.N", "ai"};
        if ("anitem".equals(c)) return new String[]{"nro.N", "aj"};
        if ("alogin".equals(c)) return new String[]{"nro.e", "c"};
        if ("sz".equals(c)) return new String[]{"nro.e", "d"};
        if ("checklag".equals(c)) return new String[]{"nro.w", "F"};
        if ("ahs".equals(c)) return new String[]{"nro.F", "R"};
        if ("kvt".equals(c)) return new String[]{"nro.h", "j"};
        if ("nb".equals(c)) return new String[]{"nro.H", "T"};
        if ("cflag".equals(c)) return new String[]{"nro.V", "aq"};
        if ("ak".equals(c)) return new String[]{"nro.n", "x"};
        return null;
    }

    public static Boolean getModFlag(String cmd) {
        try {
            String[] target = resolveModFlagTarget(cmd);
            if (target == null) return null;
            Class<?> cls = getGameClass(target[0]);
            if (cls == null) return null;
            return cls.getField(target[1]).getBoolean(null);
        } catch (Throwable ignored) {
            return null;
        }
    }

    // Idempotently set a mod boolean feature to desired state without accidental toggle-off
    public static boolean setModFlag(String cmd, boolean desired) {
        try {
            String c = cmd.trim().toLowerCase(Locale.ROOT);
            // Special dependency: tdlt requires gb (nro.A.J == true) before enabling
            if ("tdlt".equals(c) && desired) {
                setModFlag("gb", true);
            }

            Boolean cur = getModFlag(c);
            if (cur != null && cur.booleanValue() != desired) {
                executeModAction(c);
                Thread.sleep(80);
            }

            // Verify and enforce directly on the static field
            String[] target = resolveModFlagTarget(c);
            if (target != null) {
                Class<?> cls = getGameClass(target[0]);
                if (cls != null) {
                    Field f = cls.getField(target[1]);
                    if (f.getBoolean(null) != desired) {
                        f.setBoolean(null, desired);
                    }
                }
            }

            // If enabling gb, also lock in the goback map & zone
            if ("gb".equals(c) && desired) {
                updateGobackTarget(autoFarmMap, autoFarmZone);
            }
            return true;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] setModFlag error for [" + cmd + "]: " + t);
            return false;
        }
    }

    public static void updateGobackTarget(int prefMap, int prefZone) {
        try {
            Class<?> clsA = getGameClass("nro.A");
            Class<?> clsAv = getGameClass("nro.av");
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsA == null) return;

            int mapToSave = prefMap;
            if (mapToSave <= 0 && clsAv != null) {
                mapToSave = clsAv.getField("h").getInt(null);
            }
            int zoneToSave = prefZone;
            if (zoneToSave < 0 && clsCd != null) {
                Object myChar = clsCd.getMethod("b").invoke(null);
                if (myChar != null) {
                    zoneToSave = myChar.getClass().getField("hH").getInt(myChar);
                }
            }
            if (mapToSave > 0) {
                clsA.getField("aj").setInt(null, mapToSave);
            }
            if (zoneToSave >= 0) {
                clsA.getField("al").setInt(null, zoneToSave);
            }
        } catch (Throwable ignored) {}
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

    // Direct JVM Login & Server Switch (No emulator restart required)
    public static boolean directLogin(final String username, final String password, final int serverNum) {
        try {
            final Class<?> clsAg = getGameClass("nro.ag");
            final Class<?> clsAk = getGameClass("nro.ak");
            final Class<?> clsCr = getGameClass("nro.cr");
            final Class<?> clsCJ = getGameClass("nro.cJ");
            final Class<?> clsE = getGameClass("nro.e");
            if (clsAg == null || clsAk == null || clsE == null) {
                return false;
            }

            final String cleanUser = (username != null) ? username.trim().toLowerCase(Locale.ROOT) : "";
            final String cleanPass = (password != null) ? password.trim() : "";

            // 1. Persist credentials & server index inside active JVM RecordStore via nro.ag
            Method saveStr = clsAg.getMethod("b", String.class, String.class);
            Method saveInt = clsAg.getMethod("a", String.class, int.class);
            if (!cleanUser.isEmpty()) {
                saveStr.invoke(null, "acc", cleanUser);
            }
            if (!cleanPass.isEmpty()) {
                saveStr.invoke(null, "pass", cleanPass);
            }
            saveInt.invoke(null, "check", 1);

            if (serverNum >= 1 && serverNum <= 13) {
                int svIdx = serverNum - 1;
                clsAk.getField("dn").setInt(null, svIdx);
                saveInt.invoke(null, "svselect", svIdx);
            }

            // 2. Spawn asynchronous worker inside JVM to disconnect old session and trigger LoginScr login
            Thread loginWorker = new Thread(new Runnable() {
                public void run() {
                    try {
                        stopXmap();
                        // Disconnect active session & switch to LoginScr
                        clsE.getMethod("h").invoke(null);
                        Thread.sleep(350);

                        // Populate LoginScr TFields (b=user, e=pass) if present
                        if (clsCr != null && clsCJ != null) {
                            Object loginScr = clsCr.getField("a").get(null);
                            if (loginScr == null) {
                                loginScr = clsCJ.getDeclaredConstructor().newInstance();
                                clsCr.getField("a").set(null, loginScr);
                            }
                            if (!cleanUser.isEmpty()) {
                                Object tfUser = clsCJ.getField("b").get(loginScr);
                                if (tfUser != null) {
                                    tfUser.getClass().getMethod("t", String.class).invoke(tfUser, cleanUser);
                                }
                            }
                            if (!cleanPass.isEmpty()) {
                                Object tfPass = clsCJ.getField("e").get(loginScr);
                                if (tfPass != null) {
                                    tfPass.getClass().getMethod("t", String.class).invoke(tfPass, cleanPass);
                                }
                            }
                        }

                        // Trigger LoginScr.cR() + LoginScr.hS() via nro.e.j()
                        clsE.getMethod("j").invoke(null);
                        System.out.println("[BRIDGE] Direct JVM Login triggered for user=" + cleanUser + ", server=" + serverNum);

                        // Wait up to 4.5s; if still not in_game, trigger hS() once more
                        for (int i = 0; i < 9; i++) {
                            Thread.sleep(500);
                            Map<String, Object> st = getStatus();
                            if (Boolean.TRUE.equals(st.get("in_game"))) {
                                return;
                            }
                        }
                        clsE.getMethod("j").invoke(null);
                    } catch (Throwable t) {
                        System.out.println("[BRIDGE] DirectLogin-Worker error: " + t);
                    }
                }
            }, "NRO-DirectLogin-Worker");
            loginWorker.setDaemon(true);
            loginWorker.start();
            return true;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] directLogin error: " + t);
            return false;
        }
    }

    // Direct JVM Canvas Framebuffer Screenshot (240x360 JPEG)
    private static volatile Component cachedCanvasComp = null;

    private static Component findDisplayCanvas(Container root) {
        if (root == null) return null;
        String clsName = root.getClass().getName();
        if (clsName.contains("SwingDisplayComponent") || (root.getWidth() == 240 && root.getHeight() == 360)) {
            return root;
        }
        Component[] children = root.getComponents();
        for (int i = 0; i < children.length; i++) {
            Component c = children[i];
            String cName = c.getClass().getName();
            if (cName.contains("SwingDisplayComponent") || (c.getWidth() == 240 && c.getHeight() == 360)) {
                return c;
            }
            if (c instanceof Container) {
                Component found = findDisplayCanvas((Container) c);
                if (found != null) return found;
            }
        }
        return null;
    }

    public static byte[] captureScreenshotJpeg() {
        try {
            Component comp = cachedCanvasComp;
            if (comp == null || !comp.isDisplayable() || comp.getWidth() <= 0 || comp.getHeight() <= 0) {
                Window[] windows = Window.getWindows();
                for (int i = 0; i < windows.length; i++) {
                    if (windows[i].isShowing()) {
                        comp = findDisplayCanvas(windows[i]);
                        if (comp != null) {
                            cachedCanvasComp = comp;
                            break;
                        }
                    }
                }
            }
            if (comp == null) return null;

            int w = Math.max(240, comp.getWidth());
            int h = Math.max(360, comp.getHeight());
            BufferedImage img = new BufferedImage(240, 360, BufferedImage.TYPE_INT_RGB);
            Graphics2D g2 = img.createGraphics();
            comp.paint(g2);
            g2.dispose();

            ByteArrayOutputStream baos = new ByteArrayOutputStream(16384);
            ImageIO.write(img, "jpg", baos);
            byte[] bytes = baos.toByteArray();
            return (bytes != null && bytes.length > 200) ? bytes : null;
        } catch (Throwable ignored) {
            return null;
        }
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

    // Hùng Hero's auto-farm needs the primary melee/punch skill added to its auto-skill list (nro.j.a).
    // In NRO, basic melee skills are Dragon (id 0), Demon (id 2), Galick (id 4), or Liên hoàn (id 17).
    // Resolve its 1-based skill number from the active character's skill array and add it via "skill<N> 1".
    public static int configurePunchSkill() {
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCt = getGameClass("nro.ct");
            Class<?> clsJ = getGameClass("nro.j");
            if (clsCr == null || clsCt == null || clsJ == null) return -1;

            boolean alternateSkillSet = clsCr.getField("cS").getBoolean(null);
            Object array = clsCt.getField(alternateSkillSet ? "c" : "d").get(null);
            if (!(array instanceof Object[])) return -1;
            Object[] skills = (Object[]) array;

            // Check if primary array has any non-null skill; if not, fallback to the other array
            boolean hasAny = false;
            for (int i = 0; i < skills.length; i++) {
                if (skills[i] != null) { hasAny = true; break; }
            }
            if (!hasAny) {
                Object altArray = clsCt.getField(alternateSkillSet ? "d" : "c").get(null);
                if (altArray instanceof Object[]) skills = (Object[]) altArray;
            }

            int targetIdx = -1;
            // Pass 1: Match basic melee skill by template ID (0=Dragon, 2=Demon, 4=Galick) or name
            for (int i = 0; i < skills.length; i++) {
                Object skill = skills[i];
                if (skill == null) continue;
                Object template = skill.getClass().getField("a").get(skill);
                if (template == null) continue;
                int skillId = ((Number) template.getClass().getField("S").get(template)).intValue();
                Object rawName = template.getClass().getField("b").get(template);
                String normalized = (rawName instanceof String)
                    ? Normalizer.normalize((String) rawName, Normalizer.Form.NFD)
                        .replaceAll("\\p{M}+", "")
                        .replace('đ', 'd').replace('Đ', 'D')
                        .trim().toLowerCase(Locale.ROOT)
                    : "";

                if (skillId == 0 || skillId == 2 || skillId == 4
                    || normalized.startsWith("dragon")
                    || normalized.startsWith("demon")
                    || normalized.startsWith("galick")
                    || normalized.equals("dam") || normalized.startsWith("dam ") || normalized.startsWith("dam-")
                    || normalized.startsWith("lien hoan")) {
                    targetIdx = i;
                    break;
                }
            }

            // Pass 2: Fallback to the first available skill in slot 1 (index 0)
            if (targetIdx < 0) {
                for (int i = 0; i < skills.length; i++) {
                    if (skills[i] != null) {
                        targetIdx = i;
                        break;
                    }
                }
            }

            if (targetIdx < 0) return -1;

            Object skill = skills[targetIdx];
            Object template = skill.getClass().getField("a").get(skill);
            int skillId = ((Number) template.getClass().getField("S").get(template)).intValue();
            Object rawName = template.getClass().getField("b").get(template);
            int skillNumber = targetIdx + 1;

            Object rawAutoSkills = clsJ.getField("a").get(null);
            if (!(rawAutoSkills instanceof Vector)) return -1;
            Vector autoSkills = (Vector) rawAutoSkills;

            if (containsAutoSkill(autoSkills, skillId)) {
                return skillNumber;
            }

            // Use Hùng Hero's "skill<N> 1" syntax to assign skill N into auto slot 1
            for (int attempt = 0; attempt < 2; attempt++) {
                executeModAction("skill" + skillNumber + " 1");
                if (containsAutoSkill(autoSkills, skillId)) {
                    System.out.println("[BRIDGE] Selected Hùng Hero melee skill [" + rawName + "] (skill" + skillNumber + " -> slot 1) in auto combat.");
                    return skillNumber;
                }
                Thread.sleep(150);
            }

            // Fallback: toggle via nro.j.d(int) or insert directly into nro.j.a and persist via nro.j.F()
            try {
                Method dMeth = clsJ.getMethod("d", int.class);
                dMeth.invoke(null, targetIdx);
            } catch (Throwable ignored) {}

            if (!containsAutoSkill(autoSkills, skillId)) {
                autoSkills.insertElementAt(skill, 0);
                try {
                    Method fMeth = clsJ.getMethod("F");
                    fMeth.invoke(null);
                } catch (Throwable ignored) {}
            }

            if (containsAutoSkill(autoSkills, skillId)) {
                System.out.println("[BRIDGE] Guaranteed melee skill [" + rawName + "] (skill" + skillNumber + ") in auto-skill list.");
                return skillNumber;
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] configurePunchSkill error: " + t);
        }
        return -1;
    }

    private static boolean containsAutoSkill(Vector autoSkills, int skillId) {
        try {
            for (int i = 0; i < autoSkills.size(); i++) {
                Object skill = autoSkills.elementAt(i);
                if (skill == null) continue;
                Object template = skill.getClass().getField("a").get(skill);
                if (template == null) continue;
                Object rawId = template.getClass().getField("S").get(template);
                if (((Number) rawId).intValue() == skillId) return true;
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] Auto-skill verification error: " + t);
        }
        return false;
    }

    // 24/7 Watchdog Thread: Ensures Auto-Reconnect (alogin + sz) and Auto-Resume Farm after disconnect or restart
    private static void startWatchdogThread() {
        Thread wd = new Thread(new Runnable() {
            public void run() {
                System.out.println("[BRIDGE] 24/7 Auto-Reconnect & AutoFarm Watchdog started.");
                while (running) {
                    try {
                        Thread.sleep(8000);
                        if (!autoFarmActive) continue;

                        // Always keep Hùng Hero's built-in Auto-Login (nro.e.c) and Save-Zone (nro.e.d) active
                        setModFlag("alogin", true);
                        setModFlag("sz", true);
                        setModFlag("checklag", true);

                        // If an explicit farmThread is currently executing travel/setup, let it finish
                        if (farmThread != null && farmThread.isAlive()) continue;

                        Map<String, Object> st = getStatus();
                        boolean inGame = Boolean.TRUE.equals(st.get("in_game"));
                        if (!inGame) {
                            continue;
                        }

                        // Ensure melee skill is in auto list
                        configurePunchSkill();

                        Boolean xmapRun = (Boolean) st.get("xmap_running");
                        if (Boolean.TRUE.equals(xmapRun)) continue;

                        Integer curMap = (Integer) st.get("map_id");
                        Integer curZone = (Integer) st.get("zone");

                        // Check if character is dead (let Goback handle healing & returning)
                        Class<?> clsF = getGameClass("nro.F");
                        if (clsF != null) {
                            Boolean isDead = (Boolean) clsF.getMethod("d").invoke(null);
                            if (Boolean.TRUE.equals(isDead)) continue;
                        }

                        // 1. If target map is configured and we are not there, trigger Xmap travel (with 20s cooldown)
                        if (autoFarmMap > 0 && (curMap == null || curMap.intValue() != autoFarmMap)) {
                            long now = System.currentTimeMillis();
                            if (now - lastWatchdogTravelMs > 20000L) {
                                lastWatchdogTravelMs = now;
                                System.out.println("[WATCHDOG] Character at map " + curMap + " (target=" + autoFarmMap + "). Triggering Xmap...");
                                travelToMap(autoFarmMap);
                            }
                            continue;
                        }

                        // 2. If at target map, verify target zone
                        if (autoFarmZone >= 0 && curZone != null && curZone.intValue() != autoFarmZone) {
                            System.out.println("[WATCHDOG] Switching zone from " + curZone + " -> " + autoFarmZone);
                            executeModAction("k" + autoFarmZone);
                            Thread.sleep(1000);
                        }

                        // 3. Ensure Goback + TDLT/TS + Lọc đồ + Auto nhặt are ON
                        setModFlag("gb", true);
                        updateGobackTarget(autoFarmMap, autoFarmZone);
                        if ("ts".equalsIgnoreCase(autoFarmMode)) {
                            setModFlag("ts", true);
                        } else if ("tdlt".equalsIgnoreCase(autoFarmMode)) {
                            setModFlag("tdlt", true);
                        } else {
                            setModFlag("tdlt", true);
                            setModFlag("ts", true);
                        }
                        setModFlag("locdo", true);
                        setModFlag("anhat", true);
                    } catch (Throwable ignored) {}
                }
            }
        }, "NRO-AutoFarm-Watchdog");
        wd.setDaemon(true);
        wd.start();
    }

    // Scan an item array (bag or body) for SKH items
    private static void scanItemsForSkh(Object[] items, String locationLabel, List<String> skhList) {
        if (items == null) return;
        try {
            Class<?> clsCD = getGameClass("nro.cD");
            Method isSkhMeth = (clsCD != null) ? clsCD.getMethod("b", clsCD) : null;

            for (int i = 0; i < items.length; i++) {
                Object item = items[i];
                if (item == null) continue;
                Object template = item.getClass().getField("a").get(item);
                if (template == null) continue;

                boolean isSkh = false;
                if (isSkhMeth != null) {
                    isSkh = Boolean.TRUE.equals(isSkhMeth.invoke(null, item));
                }

                String itemName = String.valueOf(template.getClass().getField("b").get(template));
                Object rawOpts = item.getClass().getField("b").get(item);
                StringBuilder skhDesc = new StringBuilder();

                if (rawOpts instanceof Object[]) {
                    Object[] opts = (Object[]) rawOpts;
                    for (int j = 0; j < opts.length; j++) {
                        Object opt = opts[j];
                        if (opt == null) continue;
                        Object optTpl = opt.getClass().getField("a").get(opt);
                        if (optTpl == null) continue;
                        int optId = optTpl.getClass().getField("hQ").getInt(optTpl);
                        if (optId >= 127 && optId <= 144) {
                            isSkh = true;
                            try {
                                Object formatted = opt.getClass().getMethod("e").invoke(opt);
                                if (formatted != null) {
                                    if (skhDesc.length() > 0) skhDesc.append(", ");
                                    skhDesc.append(formatted.toString());
                                }
                            } catch (Throwable ignored) {
                                Object rawOptName = optTpl.getClass().getField("b").get(optTpl);
                                if (rawOptName != null) {
                                    if (skhDesc.length() > 0) skhDesc.append(", ");
                                    skhDesc.append(rawOptName.toString());
                                }
                            }
                        }
                    }
                }

                if (isSkh) {
                    String entry = itemName + (skhDesc.length() > 0 ? " [" + skhDesc.toString() + "]" : " [SKH]") + " (" + locationLabel + ")";
                    skhList.add(entry);
                }
            }
        } catch (Throwable ignored) {}
    }

    // Get current game status & live character telemetry
    public static Map<String, Object> getStatus() {
        Map<String, Object> status = new LinkedHashMap<String, Object>();
        status.put("bridge_online", true);
        status.put("autofarm_active", autoFarmActive);
        status.put("autofarm_map", autoFarmMap);
        status.put("autofarm_zone", autoFarmZone);
        status.put("autofarm_mode", autoFarmMode);

        try {
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                Field fE = cls_f.getField("e");
                Field fL = cls_f.getField("l");
                status.put("xmap_running", fE.getBoolean(null));
                status.put("target_map", fL.getInt(null));
            }
        } catch (Throwable ignored) {}

        int currentMapId = -1;
        try {
            Class<?> cls_av = getGameClass("nro.av");
            if (cls_av != null) {
                Field fH = cls_av.getField("h");
                Field fG = cls_av.getField("g");
                currentMapId = fH.getInt(null);
                String[] mapNames = (String[]) fG.get(null);
                String currentMapName = (mapNames != null && currentMapId >= 0 && currentMapId < mapNames.length) ? mapNames[currentMapId] : "Unknown";
                status.put("map_id", currentMapId);
                status.put("map_name", currentMapName);
            }
        } catch (Throwable ignored) {}

        boolean inGame = false;
        try {
            Class<?> cls_cd = getGameClass("nro.cd");
            if (cls_cd != null) {
                Method bMeth = cls_cd.getMethod("b");
                Object charObj = bMeth.invoke(null);
                if (charObj != null) {
                    Class<?> cCls = charObj.getClass();
                    String charName = (String) cCls.getField("gu").get(charObj);
                    int zone = cCls.getField("hH").getInt(charObj);
                    long hp = cCls.getField("aN").getLong(charObj);
                    long hpMax = cCls.getField("az").getLong(charObj);
                    long mp = cCls.getField("ax").getLong(charObj);
                    long mpMax = cCls.getField("as").getLong(charObj);
                    long power = cCls.getField("aQ").getLong(charObj);
                    long potential = cCls.getField("aO").getLong(charObj);
                    long damage = cCls.getField("aw").getLong(charObj);
                    long gold = cCls.getField("av").getLong(charObj);
                    boolean hasPet = cCls.getField("bQ").getBoolean(charObj);
                    int cx = cCls.getField("gv").getInt(charObj);
                    int cy = cCls.getField("gA").getInt(charObj);

                    inGame = (charName != null && !charName.trim().isEmpty() && currentMapId >= 0 && hpMax > 0);
                    status.put("char_name", charName != null ? charName : "");
                    status.put("zone", zone);
                    status.put("hp", hp);
                    status.put("hp_max", hpMax);
                    status.put("mp", mp);
                    status.put("mp_max", mpMax);
                    status.put("power", power);
                    status.put("potential", potential);
                    status.put("damage", damage);
                    status.put("gold", gold);
                    status.put("has_pet", hasPet);
                    status.put("cx", cx);
                    status.put("cy", cy);

                    if (hasPet) {
                        try {
                            Object petObj = cls_cd.getMethod("c").invoke(null);
                            if (petObj != null) {
                                status.put("pet_power", petObj.getClass().getField("aQ").getLong(petObj));
                                status.put("pet_potential", petObj.getClass().getField("aO").getLong(petObj));
                            }
                        } catch (Throwable ignored) {}
                    }

                    // Scan bag and body for SKH items and slot usage
                    List<String> skhList = new ArrayList<String>();
                    Object rawBody = cCls.getField("a").get(charObj);
                    if (rawBody instanceof Object[]) {
                        scanItemsForSkh((Object[]) rawBody, "Đang mặc", skhList);
                    }
                    Object rawBag = cCls.getField("b").get(charObj);
                    if (rawBag instanceof Object[]) {
                        Object[] bag = (Object[]) rawBag;
                        int used = 0;
                        for (int i = 0; i < bag.length; i++) {
                            if (bag[i] != null) used++;
                        }
                        status.put("bag_total", bag.length);
                        status.put("bag_used", used);
                        status.put("bag_free", Math.max(0, bag.length - used));
                        scanItemsForSkh(bag, "Hành trang", skhList);
                    }
                    status.put("skh_count", skhList.size());
                    status.put("skh_items", skhList);
                }
            }
        } catch (Throwable ignored) {}

        status.put("in_game", inGame);

        // Real-time mod boolean flags
        String[] trackedFlags = new String[]{"ts", "tdlt", "gb", "anhat", "locdo", "xdh", "annguoi", "alogin", "sz", "kvt", "nb", "cflag"};
        for (int i = 0; i < trackedFlags.length; i++) {
            String fName = trackedFlags[i];
            Boolean val = getModFlag(fName);
            status.put("mod_" + fName, Boolean.TRUE.equals(val));
        }

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
                int skillNumber = -1;
                for (int attempt = 0; attempt < 5 && skillNumber < 0; attempt++) {
                    skillNumber = configurePunchSkill();
                    if (skillNumber < 0) Thread.sleep(500);
                }
                if (skillNumber > 0) {
                    jsonResp = "{\"status\": \"ok\", \"skill\": " + skillNumber + "}";
                } else {
                    statusCode = 409;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Hùng Hero could not confirm the punch skill in the auto-skill list; auto combat was not started\"}";
                }
            } else if (path.equals("/api/game/auto_farm")) {
                // Batch auto farm sequence with smart map travel & persistent 24/7 state
                final String mapStr = params.get("map");
                final String zoneStr = params.get("zone");
                final String mode = params.get("mode") != null ? params.get("mode") : "all";

                int parsedMap = 0;
                if (mapStr != null && !mapStr.equals("xmap") && !mapStr.equals("0")) {
                    try { parsedMap = Integer.parseInt(mapStr.trim()); } catch (Throwable ignored) {}
                }
                int parsedZone = -1;
                if (zoneStr != null && !zoneStr.trim().isEmpty()) {
                    try { parsedZone = Integer.parseInt(zoneStr.trim()); } catch (Throwable ignored) {}
                }

                autoFarmActive = true;
                autoFarmMap = parsedMap;
                autoFarmZone = parsedZone;
                autoFarmMode = mode;
                saveAutoFarmState();

                if (farmThread != null && farmThread.isAlive()) {
                    farmThread.interrupt();
                }

                farmThread = new Thread(new Runnable() {
                    public void run() {
                        try {
                            // Always enable Auto-Login & Save-Zone first
                            setModFlag("alogin", true);
                            setModFlag("sz", true);
                            setModFlag("checklag", true);

                            // 1. If target map specified, check if already there
                            if (autoFarmMap > 0) {
                                Map<String, Object> curStat = getStatus();
                                Integer curMap = (Integer) curStat.get("map_id");
                                if (curMap == null || curMap.intValue() != autoFarmMap) {
                                    System.out.println("[BRIDGE] AutoFarm: Traveling to map " + autoFarmMap + "...");
                                    if (!travelToMap(autoFarmMap)) {
                                        System.out.println("[BRIDGE] AutoFarm: Could not start travel to map " + autoFarmMap + ".");
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
                                        if (cm != null && cm.intValue() == autoFarmMap && (xmapRun == null || !xmapRun)) {
                                            System.out.println("[BRIDGE] AutoFarm: Arrived at target map " + autoFarmMap + "!");
                                            arrived = true;
                                            break;
                                        }
                                    }
                                    if (!arrived) {
                                        System.out.println("[BRIDGE] AutoFarm: Timed out waiting for map " + autoFarmMap + "; Watchdog will retry.");
                                        return;
                                    }
                                    // Settle after arrival
                                    Thread.sleep(1200);
                                }
                            }

                            // 2. Switch zone if specified
                            if (autoFarmZone >= 0) {
                                executeModAction("k" + autoFarmZone);
                                Thread.sleep(1000);
                            }

                            // 2.5. Ensure melee/punch skill is in Hùng Hero's auto-skill list before starting combat
                            configurePunchSkill();

                            // 3. Enable Goback FIRST (required by Hùng Hero before TDLT can turn on!)
                            setModFlag("gb", true);
                            updateGobackTarget(autoFarmMap, autoFarmZone);
                            Thread.sleep(200);

                            // 4. Activate auto farm mode idempotently: TDLT + TÀN SÁT COMBO
                            if ("ts".equalsIgnoreCase(mode)) {
                                setModFlag("ts", true);
                                setModFlag("tdlt", false);
                            } else if ("tdlt".equalsIgnoreCase(mode)) {
                                setModFlag("tdlt", true);
                                setModFlag("ts", false);
                            } else {
                                // Default COMBO: activate both tdlt AND ts
                                setModFlag("tdlt", true);
                                Thread.sleep(250);
                                setModFlag("ts", true);
                            }
                            Thread.sleep(200);

                            // 5. Activate support utilities idempotently
                            setModFlag("locdo", true);
                            Thread.sleep(150);
                            setModFlag("anhat", true);
                            Thread.sleep(150);
                            executeModAction("s20");
                            Thread.sleep(150);
                            setModFlag("xdh", true);
                            Thread.sleep(150);
                            setModFlag("annguoi", true);

                            System.out.println("[BRIDGE] AutoFarm 24/7: Combo (Goback + TDLT + Tàn Sát + Lọc Đồ + Auto Nhặt + Auto Reconnect + Siêu Mượt) activated!");
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
                autoFarmActive = false;
                saveAutoFarmState();
                if (farmThread != null && farmThread.isAlive()) {
                    farmThread.interrupt();
                }
                stopXmap();
                setModFlag("ts", false);
                setModFlag("tdlt", false);
                setModFlag("gb", false);
                setModFlag("xdh", false);
                setModFlag("annguoi", false);
                jsonResp = "{\"status\": \"ok\", \"message\": \"Auto farm stopped & graphics restored\"}";
            } else if (path.equals("/api/game/login")) {
                String u = params.get("user");
                String p = params.get("pass");
                String svStr = params.get("server");
                int sv = 0;
                if (svStr != null && !svStr.trim().isEmpty()) {
                    try { sv = Integer.parseInt(svStr.trim()); } catch (Throwable ignored) {}
                }
                boolean ok = directLogin(u, p, sv);
                if (ok) {
                    jsonResp = "{\"status\": \"ok\", \"method\": \"direct_jvm\", \"server\": " + sv + "}";
                } else {
                    statusCode = 500;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Could not invoke direct JVM login\"}";
                }
            } else if (path.equals("/api/game/screenshot")) {
                byte[] imgBytes = captureScreenshotJpeg();
                if (imgBytes != null) {
                    String imgHeader = "HTTP/1.1 200 OK\r\n" +
                                       "Content-Type: image/jpeg\r\n" +
                                       "Content-Length: " + imgBytes.length + "\r\n" +
                                       "Cache-Control: no-store, no-cache\r\n" +
                                       "Connection: close\r\n\r\n";
                    out.write(imgHeader.getBytes("UTF-8"));
                    out.write(imgBytes);
                    out.flush();
                    socket.close();
                    return;
                } else {
                    statusCode = 503;
                    jsonResp = "{\"error\": \"Canvas not ready\"}";
                }
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

    private static String escapeJson(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\")
                .replace("\"", "\\\"")
                .replace("\n", "\\n")
                .replace("\r", "\\r");
    }

    private static String toJson(Map<String, Object> map) {
        StringBuilder sb = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) sb.append(",");
            first = false;
            sb.append("\"").append(escapeJson(e.getKey())).append("\":");
            Object v = e.getValue();
            if (v == null) {
                sb.append("null");
            } else if (v instanceof Number || v instanceof Boolean) {
                sb.append(v);
            } else if (v instanceof List) {
                sb.append("[");
                List<?> list = (List<?>) v;
                for (int i = 0; i < list.size(); i++) {
                    if (i > 0) sb.append(",");
                    Object item = list.get(i);
                    if (item == null) sb.append("null");
                    else if (item instanceof Number || item instanceof Boolean) sb.append(item);
                    else sb.append("\"").append(escapeJson(item.toString())).append("\"");
                }
                sb.append("]");
            } else {
                sb.append("\"").append(escapeJson(v.toString())).append("\"");
            }
        }
        sb.append("}");
        return sb.toString();
    }
}
