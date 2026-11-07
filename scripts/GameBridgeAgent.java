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
    private static volatile int autoFarmMap = -1;
    private static volatile int autoFarmZone = -1;
    private static volatile String autoFarmMode = "all";
    private static volatile long lastWatchdogTravelMs = 0L;
    private static volatile long buyingTdltSinceMs = 0L;

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
            autoFarmMap = extractIntJson(s, "map", -1);
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

    // Type-safe field lookup to handle ProGuard -overloadaggressively field collisions in NRO.jar
    private static Field getTypedField(Class<?> cls, String name, Class<?> expectedType) {
        if (cls == null || name == null || expectedType == null) return null;
        Class<?> cur = cls;
        while (cur != null) {
            Field[] fields = cur.getDeclaredFields();
            for (int i = 0; i < fields.length; i++) {
                Field f = fields[i];
                if (name.equals(f.getName()) && expectedType.equals(f.getType())) {
                    f.setAccessible(true);
                    return f;
                }
            }
            cur = cur.getSuperclass();
        }
        return null;
    }

    private static Field getTypedFieldByTypeName(Class<?> cls, String name, String expectedTypeName) {
        if (cls == null || name == null || expectedTypeName == null) return null;
        Class<?> cur = cls;
        while (cur != null) {
            Field[] fields = cur.getDeclaredFields();
            for (int i = 0; i < fields.length; i++) {
                Field f = fields[i];
                if (name.equals(f.getName())) {
                    String tName = f.getType().getTypeName();
                    String cName = f.getType().getName();
                    if (expectedTypeName.equals(tName) || expectedTypeName.equals(cName)) {
                        f.setAccessible(true);
                        return f;
                    }
                }
            }
            cur = cur.getSuperclass();
        }
        return null;
    }

    private static boolean paramsMatch(Class<?>[] actual, Class<?>[] expected) {
        int aLen = (actual == null) ? 0 : actual.length;
        int eLen = (expected == null) ? 0 : expected.length;
        if (aLen != eLen) return false;
        for (int i = 0; i < aLen; i++) {
            if (!actual[i].equals(expected[i])) return false;
        }
        return true;
    }

    // Type-and-static-safe method lookup to handle ProGuard -overloadaggressively method collisions in NRO.jar
    private static Method getTypedMethod(Class<?> cls, String name, boolean wantStatic, Class<?> expectedReturnType, Class<?>... paramTypes) {
        if (cls == null || name == null) return null;
        Class<?> cur = cls;
        while (cur != null) {
            Method[] methods = cur.getDeclaredMethods();
            for (int i = 0; i < methods.length; i++) {
                Method m = methods[i];
                if (name.equals(m.getName())
                    && Modifier.isStatic(m.getModifiers()) == wantStatic
                    && (expectedReturnType == null || expectedReturnType.equals(m.getReturnType()))
                    && paramsMatch(m.getParameterTypes(), paramTypes)) {
                    m.setAccessible(true);
                    return m;
                }
            }
            cur = cur.getSuperclass();
        }
        return null;
    }

    private static Method getTypedMethodByReturnName(Class<?> cls, String name, boolean wantStatic, String expectedReturnTypeName, Class<?>... paramTypes) {
        if (cls == null || name == null || expectedReturnTypeName == null) return null;
        Class<?> cur = cls;
        while (cur != null) {
            Method[] methods = cur.getDeclaredMethods();
            for (int i = 0; i < methods.length; i++) {
                Method m = methods[i];
                if (name.equals(m.getName())
                    && Modifier.isStatic(m.getModifiers()) == wantStatic
                    && paramsMatch(m.getParameterTypes(), paramTypes)) {
                    String rTypeName = m.getReturnType().getTypeName();
                    String rClassName = m.getReturnType().getName();
                    if (expectedReturnTypeName.equals(rTypeName) || expectedReturnTypeName.equals(rClassName)) {
                        m.setAccessible(true);
                        return m;
                    }
                }
            }
            cur = cur.getSuperclass();
        }
        return null;
    }

    // Safely retrieve myCharZ singleton (static nro.cd b() or static field nro.cd l)
    private static Object getMyChar() {
        try {
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsCd == null) return null;
            Method mB = getTypedMethodByReturnName(clsCd, "b", true, "nro.cd");
            if (mB != null) {
                Object c = mB.invoke(null);
                if (c != null) return c;
            }
            Field fL = getTypedFieldByTypeName(clsCd, "l", "nro.cd");
            if (fL != null) {
                return fL.get(null);
            }
        } catch (Throwable ignored) {}
        return null;
    }

    // Safely retrieve myPet singleton (static nro.cd c() or static field nro.cd c)
    private static Object getMyPet() {
        try {
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsCd == null) return null;
            Method mC = getTypedMethodByReturnName(clsCd, "c", true, "nro.cd");
            if (mC != null) {
                Object p = mC.invoke(null);
                if (p != null) return p;
            }
            Field fC = getTypedFieldByTypeName(clsCd, "c", "nro.cd");
            if (fC != null) {
                return fC.get(null);
            }
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
            Field f = getTypedField(cls, target[1], boolean.class);
            if (f == null) return null;
            return f.getBoolean(null);
        } catch (Throwable ignored) {
            return null;
        }
    }

    // Set or clear in-game TDLT flag (nro.ct.cT), use TDLT item 521 from bag, or trigger auto-buy at Map 5 (Đảo Kame)
    private static void syncInGameTdltState(boolean enable) {
        try {
            Class<?> clsCt = getGameClass("nro.ct");
            Class<?> clsP = getGameClass("nro.p");
            Class<?> clsF = getGameClass("nro.F");
            if (!enable) {
                if (clsP != null) {
                    Field fB = getTypedField(clsP, "B", boolean.class);
                    if (fB != null) fB.setBoolean(null, false);
                }
                if (clsCt != null) {
                    Field fCt = getTypedField(clsCt, "cT", boolean.class);
                    if (fCt != null) fCt.setBoolean(null, false);
                }
                return;
            }

            // Ensure nro.F.aL >= 1 so nro.p.ad() also auto-renews TDLT when <= 1 minute remains
            if (clsF != null) {
                Field fAl = getTypedField(clsF, "aL", int.class);
                if (fAl != null && fAl.getInt(null) < 1) {
                    fAl.setInt(null, 1);
                }
            }

            // If nro.p.B is already true (currently flying to Map 5 Đảo Kame to buy TDLT from Santa), let it finish
            if (clsP != null) {
                Field fB = getTypedField(clsP, "B", boolean.class);
                if (fB != null && fB.getBoolean(null)) {
                    return;
                }
            }

            // Check if TDLT buff icon is active via nro.p.b()
            int tdltMinutes = -1;
            if (clsP != null) {
                try {
                    Method mB = getTypedMethod(clsP, "b", true, int.class);
                    if (mB != null) {
                        Object res = mB.invoke(null);
                        if (res instanceof Number) tdltMinutes = ((Number) res).intValue();
                    }
                } catch (Throwable ignored) {}
            }

            // If TDLT buff is not active yet (tdltMinutes == -1), try using item 521 from bag or auto-buying at Map 5
            if (tdltMinutes == -1) {
                int slot = -1;
                try {
                    Class<?> clsCD = getGameClass("nro.cD");
                    Class<?> clsX = getGameClass("nro.X");
                    if (clsCD != null && clsX != null) {
                        Method mFind = getTypedMethod(clsCD, "b", true, int.class, short.class);
                        if (mFind != null) {
                            slot = ((Number) mFind.invoke(null, (short) 521)).intValue();
                        }
                        if (slot != -1) {
                            Method mUse = getTypedMethod(clsX, "k", true, void.class, int.class);
                            if (mUse != null) {
                                mUse.invoke(null, slot);
                                System.out.println("[BRIDGE] Activated TDLT item 521 from bag slot " + slot);
                            }
                        }
                    }
                } catch (Throwable ignored) {}

                // If no item 521 in bag (slot == -1), check if character has gems (myChar.F() > 0) to auto-buy at Map 5 (Đảo Kame)
                if (slot == -1) {
                    Object myChar = getMyChar();
                    Class<?> clsCd = getGameClass("nro.cd");
                    int totalGems = 0;
                    if (myChar != null && clsCd != null) {
                        try {
                            Method mGems = getTypedMethod(clsCd, "F", false, int.class);
                            if (mGems != null) {
                                Object gObj = mGems.invoke(myChar);
                                if (gObj instanceof Number) totalGems = ((Number) gObj).intValue();
                            }
                        } catch (Throwable ignored) {}
                    }

                    if (totalGems > 0 && clsP != null) {
                        updateGobackTarget(autoFarmMap, autoFarmZone);
                        Field fB = getTypedField(clsP, "B", boolean.class);
                        Field fC = getTypedField(clsP, "C", boolean.class);
                        Field fP = getTypedField(clsP, "P", int.class);
                        if (fB != null) fB.setBoolean(null, true);
                        if (fC != null) fC.setBoolean(null, false);
                        if (fP != null) fP.setInt(null, 0);
                        buyingTdltSinceMs = System.currentTimeMillis();

                        if (clsCt != null) {
                            Field fCt = getTypedField(clsCt, "cT", boolean.class);
                            if (fCt != null) fCt.setBoolean(null, false);
                            Method mInstCt = getTypedMethodByReturnName(clsCt, "a", true, "nro.ct");
                            if (mInstCt != null) {
                                Object instCt = mInstCt.invoke(null);
                                if (instCt != null) {
                                    Field fIt = getTypedField(clsCt, "iT", int.class);
                                    if (fIt != null) fIt.setInt(instCt, 0);
                                }
                            }
                        }

                        Class<?> cls_f = getGameClass("nro.f");
                        if (cls_f != null) {
                            Field fF = getTypedField(cls_f, "f", boolean.class);
                            if (fF != null) fF.setBoolean(null, false);
                            Field fG = getTypedField(cls_f, "g", boolean.class);
                            if (fG != null) fG.setBoolean(null, false);
                            Field fH = getTypedField(cls_f, "h", boolean.class);
                            if (fH != null) fH.setBoolean(null, false);
                            Field fDelay = getTypedField(cls_f, "c", long.class);
                            if (fDelay != null) fDelay.setLong(null, 0L);
                            Method bMeth = getTypedMethod(cls_f, "b", true, void.class, int.class);
                            if (bMeth != null) bMeth.invoke(null, 5);
                        }
                        System.out.println("[BRIDGE] No TDLT buff or item 521 in bag (gems=" + totalGems + "). Flying to Map 5 (Đảo Kame) to auto-buy TDLT!");
                        return;
                    }
                }
            }

            if (clsCt != null) {
                Field fCt = getTypedField(clsCt, "cT", boolean.class);
                if (fCt != null) fCt.setBoolean(null, true);
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] syncInGameTdltState error: " + t);
        }
    }

    // Idempotently set a mod boolean feature to desired state without accidental toggle-off
    public static boolean setModFlag(String cmd, boolean desired) {
        try {
            String c = cmd.trim().toLowerCase(Locale.ROOT);
            // Special dependency: tdlt requires gb (nro.A.J == true) and target map locked before enabling
            if ("tdlt".equals(c) && desired) {
                setModFlag("gb", true);
                updateGobackTarget(autoFarmMap, autoFarmZone);
            }
            // Special dependency: ts requires a valid auto-skill in nro.j.a before enabling
            if ("ts".equals(c) && desired) {
                configurePunchSkill();
            }

            Boolean cur = getModFlag(c);
            if (cur != null && cur.booleanValue() != desired) {
                executeModAction(c);
            }

            // Verify and enforce directly on the typed boolean static field
            String[] target = resolveModFlagTarget(c);
            if (target != null) {
                Class<?> cls = getGameClass(target[0]);
                if (cls != null) {
                    Field f = getTypedField(cls, target[1], boolean.class);
                    if (f != null && f.getBoolean(null) != desired) {
                        f.setBoolean(null, desired);
                    }
                }
            }

            // If enabling gb, always lock in the goback map & zone
            if ("gb".equals(c) && desired) {
                updateGobackTarget(autoFarmMap, autoFarmZone);
            }
            // Sync in-game TDLT state (nro.ct.cT) when toggling tdlt
            if ("tdlt".equals(c)) {
                syncInGameTdltState(desired);
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
            if (clsA == null) return;

            int mapToSave = prefMap;
            if (mapToSave < 0 && clsAv != null) {
                Field fH = getTypedField(clsAv, "h", int.class);
                if (fH != null) mapToSave = fH.getInt(null);
            }
            int zoneToSave = prefZone;
            if (zoneToSave < 0 && clsAv != null) {
                Field fJ = getTypedField(clsAv, "j", int.class);
                if (fJ != null) zoneToSave = fJ.getInt(null);
            }
            Field fAj = getTypedField(clsA, "aj", int.class);
            if (fAj != null && mapToSave >= 0) {
                fAj.setInt(null, mapToSave);
            }
            Field fAl = getTypedField(clsA, "al", int.class);
            if (fAl != null && zoneToSave >= 0) {
                fAl.setInt(null, zoneToSave);
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] updateGobackTarget error: " + t);
        }
    }

    // Pause conflicting combat/TDLT/goback loops so Xmap can walk/teleport through portals unobstructed
    public static void pauseAutoForTravel(int targetMapId) {
        try {
            // 1. Pause Tàn Sát (nro.j.n = false)
            Class<?> clsJ = getGameClass("nro.j");
            if (clsJ != null) {
                Field fN = getTypedField(clsJ, "n", boolean.class);
                if (fN != null) fN.setBoolean(null, false);
            }
            // 2. Pause Mod TDLT (nro.p.z = false, nro.p.B = false) so nro.p.ad() doesn't re-enable cT or hijack Xmap to Map 5
            Class<?> clsP = getGameClass("nro.p");
            if (clsP != null) {
                Field fZ = getTypedField(clsP, "z", boolean.class);
                if (fZ != null) fZ.setBoolean(null, false);
                Field fB = getTypedField(clsP, "B", boolean.class);
                if (fB != null) fB.setBoolean(null, false);
            }
            // 3. Pause Goback during transit (nro.A.J = false, nro.A.I = false) and pre-set new target map (nro.A.aj)
            Class<?> clsA = getGameClass("nro.A");
            if (clsA != null) {
                Field fJ = getTypedField(clsA, "J", boolean.class);
                if (fJ != null) fJ.setBoolean(null, false);
                Field fI = getTypedField(clsA, "I", boolean.class);
                if (fI != null) fI.setBoolean(null, false);
                if (targetMapId >= 0) {
                    Field fAj = getTypedField(clsA, "aj", int.class);
                    if (fAj != null) fAj.setInt(null, targetMapId);
                }
            }
            // 4. Turn off Khóa Vị Trí (nro.h.j = false) so nro.h.w() doesn't teleport char back to old X/Y on starting map
            Class<?> clsH = getGameClass("nro.h");
            if (clsH != null) {
                Field fKvt = getTypedField(clsH, "j", boolean.class);
                if (fKvt != null) fKvt.setBoolean(null, false);
            }
            // 5. Turn off in-game TDLT (nro.ct.cT = false, nro.ct.a().iT = 0) so GameScr stops pulling char to mobs
            Class<?> clsCt = getGameClass("nro.ct");
            if (clsCt != null) {
                Field fCt = getTypedField(clsCt, "cT", boolean.class);
                if (fCt != null) fCt.setBoolean(null, false);
                try {
                    Method mInstCt = getTypedMethodByReturnName(clsCt, "a", true, "nro.ct");
                    Object instCt = (mInstCt != null) ? mInstCt.invoke(null) : null;
                    if (instCt != null) {
                        Field fIt = getTypedField(clsCt, "iT", int.class);
                        if (fIt != null) fIt.setInt(instCt, 0);
                    }
                } catch (Throwable ignored) {}
            }
            // 6. Clear active character auto-move target (nro.cQ a) and mob focus (nro.cN e)
            Class<?> clsCd = getGameClass("nro.cd");
            Object myChar = getMyChar();
            if (clsCd != null && myChar != null) {
                Field fMove = getTypedFieldByTypeName(clsCd, "a", "nro.cQ");
                if (fMove != null) fMove.set(myChar, null);
                Field fMob = getTypedFieldByTypeName(clsCd, "e", "nro.cN");
                if (fMob != null) fMob.set(myChar, null);
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] pauseAutoForTravel error: " + t);
        }
    }

    // Call Xmap travel to mapId
    public static boolean travelToMap(int mapId) {
        try {
            pauseAutoForTravel(mapId);
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                // Reset any stale Xmap error/capsule/delay flags before starting new route
                Field fF = getTypedField(cls_f, "f", boolean.class);
                if (fF != null) fF.setBoolean(null, false);
                Field fG = getTypedField(cls_f, "g", boolean.class);
                if (fG != null) fG.setBoolean(null, false);
                Field fH = getTypedField(cls_f, "h", boolean.class);
                if (fH != null) fH.setBoolean(null, false);
                Field fC = getTypedField(cls_f, "c", long.class);
                if (fC != null) fC.setLong(null, 0L);

                Method bMeth = getTypedMethod(cls_f, "b", true, void.class, int.class);
                if (bMeth != null) {
                    bMeth.invoke(null, mapId);
                    System.out.println("[BRIDGE] Travel to map " + mapId + " triggered via nro.f.b(" + mapId + ")");
                    return true;
                }
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
                Field fE = getTypedField(cls_f, "e", boolean.class);
                if (fE != null) fE.setBoolean(null, false);
                Field fF = getTypedField(cls_f, "f", boolean.class);
                if (fF != null) fF.setBoolean(null, false);
                Field fG = getTypedField(cls_f, "g", boolean.class);
                if (fG != null) fG.setBoolean(null, false);
                Field fH = getTypedField(cls_f, "h", boolean.class);
                if (fH != null) fH.setBoolean(null, false);
                Object myChar = getMyChar();
                Class<?> clsCd = getGameClass("nro.cd");
                if (clsCd != null && myChar != null) {
                    Field fMove = getTypedFieldByTypeName(clsCd, "a", "nro.cQ");
                    if (fMove != null) fMove.set(myChar, null);
                }
                System.out.println("[BRIDGE] Stop Xmap triggered");
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
            Method saveStr = getTypedMethod(clsAg, "b", true, void.class, String.class, String.class);
            Method saveInt = getTypedMethod(clsAg, "a", true, void.class, String.class, int.class);
            if (!cleanUser.isEmpty() && saveStr != null) {
                saveStr.invoke(null, "acc", cleanUser);
            }
            if (!cleanPass.isEmpty() && saveStr != null) {
                saveStr.invoke(null, "pass", cleanPass);
            }
            if (saveInt != null) {
                saveInt.invoke(null, "check", 1);
            }

            if (serverNum >= 1 && serverNum <= 13) {
                int svIdx = serverNum - 1;
                Field fDn = getTypedField(clsAk, "dn", int.class);
                if (fDn != null) fDn.setInt(null, svIdx);
                if (saveInt != null) saveInt.invoke(null, "svselect", svIdx);
            }

            // 2. Spawn asynchronous worker inside JVM to disconnect old session and trigger LoginScr login
            Thread loginWorker = new Thread(new Runnable() {
                public void run() {
                    try {
                        stopXmap();
                        // Disconnect active session & switch to LoginScr
                        Method mH = getTypedMethod(clsE, "h", true, void.class);
                        if (mH != null) mH.invoke(null);
                        Thread.sleep(350);

                        // Populate LoginScr TFields (b=user, e=pass) using type-safe reflection
                        if (clsCr != null && clsCJ != null) {
                            Field fLoginScr = getTypedFieldByTypeName(clsCr, "a", "nro.cJ");
                            if (fLoginScr != null) {
                                Object loginScr = fLoginScr.get(null);
                                if (loginScr == null) {
                                    loginScr = clsCJ.getDeclaredConstructor().newInstance();
                                    fLoginScr.set(null, loginScr);
                                }
                                if (!cleanUser.isEmpty()) {
                                    Field fUser = getTypedFieldByTypeName(clsCJ, "b", "nro.au");
                                    Object tfUser = (fUser != null) ? fUser.get(loginScr) : null;
                                    if (tfUser != null) {
                                        Method mT = getTypedMethod(tfUser.getClass(), "t", false, void.class, String.class);
                                        if (mT != null) mT.invoke(tfUser, cleanUser);
                                    }
                                }
                                if (!cleanPass.isEmpty()) {
                                    Field fPass = getTypedFieldByTypeName(clsCJ, "e", "nro.au");
                                    Object tfPass = (fPass != null) ? fPass.get(loginScr) : null;
                                    if (tfPass != null) {
                                        Method mT = getTypedMethod(tfPass.getClass(), "t", false, void.class, String.class);
                                        if (mT != null) mT.invoke(tfPass, cleanPass);
                                    }
                                }
                            }
                        }

                        // Trigger LoginScr.cR() + LoginScr.hS() via nro.e.j()
                        Method mJ = getTypedMethod(clsE, "j", true, void.class);
                        if (mJ != null) mJ.invoke(null);
                        System.out.println("[BRIDGE] Direct JVM Login triggered for user=" + cleanUser + ", server=" + serverNum);

                        // Wait up to 4.5s; if still not in_game, trigger hS() once more
                        for (int i = 0; i < 9; i++) {
                            Thread.sleep(500);
                            Map<String, Object> st = getStatus();
                            if (Boolean.TRUE.equals(st.get("in_game"))) {
                                return;
                            }
                        }
                        if (mJ != null) mJ.invoke(null);
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
        String lower = cmd.toLowerCase(Locale.ROOT);
        boolean handled = false;
        try {
            // 1. Open Xmap menu via nro.f.a().o()
            if ("xmap".equals(lower)) {
                Class<?> cls_f = getGameClass("nro.f");
                if (cls_f != null) {
                    Method mA = getTypedMethodByReturnName(cls_f, "a", true, "nro.f");
                    Object inst_f = (mA != null) ? mA.invoke(null) : null;
                    if (inst_f != null) {
                        Method mO = getTypedMethod(cls_f, "o", false, void.class);
                        if (mO != null) mO.invoke(inst_f);
                        System.out.println("[BRIDGE] Opened Xmap menu via nro.f.a().o()");
                        return true;
                    }
                }
            }

            // 2. Open Hùng Hero Settings / Lọc Đồ Menu ("cd") via nro.G.a().aK()
            if ("cd".equals(lower) || "menu".equals(lower)) {
                Class<?> clsG = getGameClass("nro.G");
                if (clsG != null) {
                    Method mA = getTypedMethodByReturnName(clsG, "a", true, "nro.G");
                    Object instG = (mA != null) ? mA.invoke(null) : null;
                    if (instG != null) {
                        Method mAk = getTypedMethod(clsG, "aK", false, void.class);
                        if (mAk != null) mAk.invoke(instG);
                        System.out.println("[BRIDGE] Opened Hùng Hero Mod Menu via nro.G.a().aK()");
                        return true;
                    }
                }
            }

            // 3. Use Capsule ("cs") via nro.G.a().a(2, null)
            if ("cs".equals(lower)) {
                Class<?> clsG = getGameClass("nro.G");
                if (clsG != null) {
                    Method mA = getTypedMethodByReturnName(clsG, "a", true, "nro.G");
                    Object instG = (mA != null) ? mA.invoke(null) : null;
                    if (instG != null) {
                        Method mAct = getTypedMethod(clsG, "a", false, void.class, int.class, Object.class);
                        if (mAct != null) mAct.invoke(instG, 2, null);
                        System.out.println("[BRIDGE] Used Capsule via nro.G.a().a(2, null)");
                        return true;
                    }
                }
            }

            // 4. Use Porata Earring ("bt" / "bongtai") via nro.G.a().a(3, null)
            if ("bt".equals(lower) || "bongtai".equals(lower)) {
                Class<?> clsG = getGameClass("nro.G");
                if (clsG != null) {
                    Method mA = getTypedMethodByReturnName(clsG, "a", true, "nro.G");
                    Object instG = (mA != null) ? mA.invoke(null) : null;
                    if (instG != null) {
                        Method mAct = getTypedMethod(clsG, "a", false, void.class, int.class, Object.class);
                        if (mAct != null) mAct.invoke(instG, 3, null);
                        System.out.println("[BRIDGE] Used Porata via nro.G.a().a(3, null)");
                        return true;
                    }
                }
            }

            // 5. If user manually toggles "tdlt" chip, ensure "gb" is enabled first so Hùng Hero accepts it
            if ("tdlt".equals(lower)) {
                Boolean curTdlt = getModFlag("tdlt");
                if (curTdlt != null && !curTdlt.booleanValue()) {
                    setModFlag("gb", true);
                    updateGobackTarget(autoFarmMap, autoFarmZone);
                }
            }

            // 6. If user manually toggles "ts" chip on, ensure punch skill is configured first
            if ("ts".equals(lower)) {
                Boolean curTs = getModFlag("ts");
                if (curTs != null && !curTs.booleanValue()) {
                    configurePunchSkill();
                }
            }

            Boolean beforeFlag = getModFlag(lower);

            // 7. Invoke the Hùng Hero command handler (nro.F.a().d(cmd) / e(cmd))
            Class<?> clsF = getGameClass("nro.F");
            if (clsF != null) {
                Method aMeth = getTypedMethodByReturnName(clsF, "a", true, "nro.F");
                Object instF = (aMeth != null) ? aMeth.invoke(null) : null;
                if (instF != null) {
                    Method dMeth = getTypedMethod(clsF, "d", false, boolean.class, String.class);
                    Object resD = (dMeth != null) ? dMeth.invoke(instF, lower) : null;
                    if (Boolean.TRUE.equals(resD)) {
                        handled = true;
                    } else {
                        try {
                            Method eMeth = getTypedMethod(clsF, "e", false, boolean.class, String.class);
                            Object resE = (eMeth != null) ? eMeth.invoke(instF, lower) : null;
                            if (Boolean.TRUE.equals(resE)) handled = true;
                        } catch (Throwable ignored) {}
                    }
                    System.out.println("[BRIDGE] Executed nro.F cmd [" + lower + "] -> " + handled);
                }
            }

            // Guarantee toggle for known boolean mod flags even if nro.ct.b chat popup threw an exception
            if (beforeFlag != null) {
                Boolean afterFlag = getModFlag(lower);
                if (!handled || beforeFlag.equals(afterFlag)) {
                    String[] target = resolveModFlagTarget(lower);
                    if (target != null) {
                        Class<?> tCls = getGameClass(target[0]);
                        if (tCls != null) {
                            Field f = getTypedField(tCls, target[1], boolean.class);
                            if (f != null) {
                                f.setBoolean(null, !beforeFlag.booleanValue());
                                handled = true;
                            }
                        }
                    }
                }
            }

            if (handled && "gb".equals(lower) && Boolean.TRUE.equals(getModFlag("gb"))) {
                updateGobackTarget(-1, -1);
            }

            if (handled && "kvt".equals(lower) && Boolean.TRUE.equals(getModFlag("kvt"))) {
                try {
                    Class<?> clsH = getGameClass("nro.h");
                    Class<?> clsAv = getGameClass("nro.av");
                    Class<?> clsCd = getGameClass("nro.cd");
                    Object myChar = getMyChar();
                    if (clsH != null && clsAv != null && clsCd != null) {
                        Field fMap = getTypedField(clsAv, "h", int.class);
                        Field fQ = getTypedField(clsH, "q", int.class);
                        Field fO = getTypedField(clsH, "o", int.class);
                        Field fP = getTypedField(clsH, "p", int.class);
                        if (fMap != null && fQ != null) fQ.setInt(null, fMap.getInt(null));
                        if (myChar != null && fO != null && fP != null) {
                            Field fCx = getTypedField(clsCd, "gv", int.class);
                            Field fCy = getTypedField(clsCd, "gA", int.class);
                            if (fCx != null) fO.setInt(null, fCx.getInt(myChar));
                            if (fCy != null) fP.setInt(null, fCy.getInt(myChar));
                        }
                    }
                } catch (Throwable ignored) {}
            }

            // Sync in-game TDLT flag if "tdlt" was toggled
            if (handled && "tdlt".equals(lower)) {
                Boolean nowTdlt = getModFlag("tdlt");
                if (nowTdlt != null) {
                    syncInGameTdltState(nowTdlt.booleanValue());
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
                Method aMeth = getTypedMethodByReturnName(clsAm, "a", true, "nro.am");
                Object instAm = (aMeth != null) ? aMeth.invoke(null) : null;
                if (instAm != null) {
                    Method pMeth = getTypedMethod(clsAm, "p", false, void.class, String.class);
                    if (pMeth != null) {
                        pMeth.invoke(instAm, cmd.trim());
                        System.out.println("[BRIDGE] Executed via nro.am.a().p [" + cmd + "]");
                        return true;
                    }
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] executeChatCmd error for [" + cmd + "]: " + t);
        }
        return false;
    }

    private static int getSkillTemplateId(Object skill) {
        if (skill == null) return -1;
        try {
            Field fA = getTypedFieldByTypeName(skill.getClass(), "a", "nro.ar");
            if (fA == null) return -1;
            Object tmpl = fA.get(skill);
            if (tmpl == null) return -1;
            Field fS = getTypedField(tmpl.getClass(), "S", byte.class);
            if (fS == null) return -1;
            return fS.getByte(tmpl);
        } catch (Throwable ignored) {
            return -1;
        }
    }

    private static String getSkillTemplateName(Object skill) {
        if (skill == null) return "";
        try {
            Field fA = getTypedFieldByTypeName(skill.getClass(), "a", "nro.ar");
            if (fA == null) return "";
            Object tmpl = fA.get(skill);
            if (tmpl == null) return "";
            Field fB = getTypedField(tmpl.getClass(), "b", String.class);
            if (fB == null) return "";
            Object name = fB.get(tmpl);
            return (name instanceof String) ? (String) name : "";
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static boolean isBasicMeleeSkill(Object skill) {
        int skillId = getSkillTemplateId(skill);
        if (skillId < 0) return false;
        String rawName = getSkillTemplateName(skill);
        String normalized = Normalizer.normalize(rawName, Normalizer.Form.NFD)
            .replaceAll("\\p{M}+", "")
            .replace('đ', 'd').replace('Đ', 'D')
            .trim().toLowerCase(Locale.ROOT);
        return skillId == 0 || skillId == 2 || skillId == 4 || skillId == 17
            || normalized.startsWith("dragon")
            || normalized.startsWith("demon")
            || normalized.startsWith("galick")
            || normalized.equals("dam") || normalized.startsWith("dam ") || normalized.startsWith("dam-")
            || normalized.startsWith("lien hoan");
    }

    // Hùng Hero's auto-farm needs the primary melee/punch skill added to its auto-skill list (nro.j.a)
    // AND present in the active hotbar (nro.ct.c / nro.ct.d), with nro.j.b drained so nro.j.E() doesn't wipe it.
    public static int configurePunchSkill() {
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCt = getGameClass("nro.ct");
            Class<?> clsJ = getGameClass("nro.j");
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsCr == null || clsCt == null || clsJ == null) return 1;

            // 1. Drain nro.j.b via nro.j.E() first so nro.j.E() never overwrites nro.j.a with nulls later
            try {
                Method mE = getTypedMethod(clsJ, "E", true, void.class);
                if (mE != null) mE.invoke(null);
            } catch (Throwable ignored) {}

            Field fAutoA = getTypedField(clsJ, "a", Vector.class);
            Field fAutoB = getTypedField(clsJ, "b", Vector.class);
            if (fAutoB != null) {
                Vector pendingB = (Vector) fAutoB.get(null);
                if (pendingB != null && pendingB.size() > 0) {
                    pendingB.removeAllElements();
                }
            }
            if (fAutoA == null) return 1;
            Vector autoSkills = (Vector) fAutoA.get(null);
            if (autoSkills == null) return 1;

            // Ensure autoSkills has exactly 10 slots as required by nro.j.F() / nro.j.I() ("listSkillsAuto")
            while (autoSkills.size() < 10) {
                autoSkills.addElement(null);
            }
            while (autoSkills.size() > 10) {
                autoSkills.removeElementAt(autoSkills.size() - 1);
            }

            Field fCs = getTypedField(clsCr, "cS", boolean.class);
            boolean alternateSkillSet = (fCs != null) && fCs.getBoolean(null);
            Field fHotbarC = getTypedFieldByTypeName(clsCt, "c", "nro.ao[]");
            Field fHotbarD = getTypedFieldByTypeName(clsCt, "d", "nro.ao[]");
            Object[] hotbarC = (fHotbarC != null) ? (Object[]) fHotbarC.get(null) : null;
            Object[] hotbarD = (fHotbarD != null) ? (Object[]) fHotbarD.get(null) : null;
            Object[] activeHotbar = alternateSkillSet ? hotbarC : hotbarD;
            Object[] backupHotbar = alternateSkillSet ? hotbarD : hotbarC;

            Object chosenSkill = null;
            int chosenIndex = 0;

            // Search activeHotbar then backupHotbar for basic melee skill
            Object[][] bars = new Object[][]{activeHotbar, backupHotbar};
            for (int b = 0; b < bars.length && chosenSkill == null; b++) {
                Object[] bar = bars[b];
                if (bar == null) continue;
                for (int i = 0; i < bar.length; i++) {
                    if (bar[i] != null && isBasicMeleeSkill(bar[i])) {
                        chosenSkill = bar[i];
                        chosenIndex = i;
                        break;
                    }
                }
            }

            // Fallback: any non-null skill in activeHotbar or backupHotbar
            for (int b = 0; b < bars.length && chosenSkill == null; b++) {
                Object[] bar = bars[b];
                if (bar == null) continue;
                for (int i = 0; i < bar.length; i++) {
                    if (bar[i] != null && getSkillTemplateId(bar[i]) >= 0) {
                        chosenSkill = bar[i];
                        chosenIndex = i;
                        break;
                    }
                }
            }

            // Fallback: character's learned skill list (nro.cd.b().L) or active skill (nro.cd.b().d)
            Object myChar = getMyChar();
            if (chosenSkill == null && myChar != null && clsCd != null) {
                Field fL = getTypedFieldByTypeName(clsCd, "L", "nro.cS");
                if (fL != null) {
                    Vector learned = (Vector) fL.get(myChar);
                    if (learned != null) {
                        for (int i = 0; i < learned.size(); i++) {
                            Object s = learned.elementAt(i);
                            if (s != null && isBasicMeleeSkill(s)) {
                                chosenSkill = s;
                                chosenIndex = 0;
                                break;
                            }
                        }
                        if (chosenSkill == null && learned.size() > 0) {
                            chosenSkill = learned.elementAt(0);
                            chosenIndex = 0;
                        }
                    }
                }
                if (chosenSkill == null) {
                    Field fD = getTypedFieldByTypeName(clsCd, "d", "nro.ao");
                    if (fD != null) {
                        chosenSkill = fD.get(myChar);
                        chosenIndex = 0;
                    }
                }
            }

            if (chosenSkill == null) return 1;

            int skillId = getSkillTemplateId(chosenSkill);
            String skillName = getSkillTemplateName(chosenSkill);

            // Ensure chosenSkill is present on both hotbarC and hotbarD so nro.j.H() always finds it
            if (hotbarC != null && hotbarC.length > 0) {
                boolean foundC = false;
                for (int i = 0; i < hotbarC.length; i++) {
                    if (hotbarC[i] != null && getSkillTemplateId(hotbarC[i]) == skillId) {
                        foundC = true;
                        break;
                    }
                }
                if (!foundC) hotbarC[0] = chosenSkill;
            }
            if (hotbarD != null && hotbarD.length > 0) {
                boolean foundD = false;
                for (int i = 0; i < hotbarD.length; i++) {
                    if (hotbarD[i] != null && getSkillTemplateId(hotbarD[i]) == skillId) {
                        foundD = true;
                        break;
                    }
                }
                if (!foundD) hotbarD[0] = chosenSkill;
            }

            // Ensure character's selected skill (nro.cd.b().d) is non-null
            if (myChar != null && clsCd != null) {
                Field fCharSkill = getTypedFieldByTypeName(clsCd, "d", "nro.ao");
                if (fCharSkill != null && fCharSkill.get(myChar) == null) {
                    fCharSkill.set(myChar, chosenSkill);
                }
            }

            // Ensure autoSkills (nro.j.a) contains chosenSkill and persist via nro.j.F()
            if (!containsAutoSkill(autoSkills, skillId)) {
                autoSkills.setElementAt(chosenSkill, 0);
                try {
                    Method mF = getTypedMethod(clsJ, "F", true, void.class);
                    if (mF != null) mF.invoke(null);
                } catch (Throwable ignored) {}
                System.out.println("[BRIDGE] Configured melee skill [" + skillName + "] (id=" + skillId + ") in nro.j.a[0]");
            }

            return chosenIndex + 1;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] configurePunchSkill error: " + t);
            return 1;
        }
    }

    private static boolean containsAutoSkill(Vector autoSkills, int skillId) {
        if (autoSkills == null || skillId < 0) return false;
        try {
            for (int i = 0; i < autoSkills.size(); i++) {
                Object skill = autoSkills.elementAt(i);
                if (skill != null && getSkillTemplateId(skill) == skillId) {
                    return true;
                }
            }
        } catch (Throwable ignored) {}
        return false;
    }

    // Apply the requested farming mode ("all", "tdlt", or "ts") immediately
    public static void applyFarmModeFlags(String mode) {
        setModFlag("gb", true);
        updateGobackTarget(autoFarmMap, autoFarmZone);
        if ("ts".equalsIgnoreCase(mode)) {
            setModFlag("tdlt", false);
            setModFlag("ts", true);
        } else if ("tdlt".equalsIgnoreCase(mode)) {
            setModFlag("ts", false);
            setModFlag("tdlt", true);
        } else {
            setModFlag("tdlt", true);
            setModFlag("ts", true);
        }
    }

    // 24/7 Watchdog Thread: Ensures Auto-Reconnect (alogin + sz) and Auto-Resume Farm after disconnect or restart
    private static void startWatchdogThread() {
        Thread wd = new Thread(new Runnable() {
            public void run() {
                System.out.println("[BRIDGE] 24/7 Auto-Reconnect & AutoFarm Watchdog started.");
                while (running) {
                    try {
                        Thread.sleep(6000);
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

                        // If currently flying to Map 5 (Đảo Kame) to auto-buy TDLT from Santa, do not interrupt!
                        Class<?> clsP = getGameClass("nro.p");
                        if (clsP != null) {
                            Field fBuyingTdlt = getTypedField(clsP, "B", boolean.class);
                            if (fBuyingTdlt != null && fBuyingTdlt.getBoolean(null)) {
                                if (buyingTdltSinceMs == 0L) buyingTdltSinceMs = System.currentTimeMillis();
                                if (System.currentTimeMillis() - buyingTdltSinceMs < 90000L) {
                                    continue;
                                } else {
                                    // Safety timeout after 90s so character never gets stuck
                                    fBuyingTdlt.setBoolean(null, false);
                                    buyingTdltSinceMs = 0L;
                                }
                            } else {
                                buyingTdltSinceMs = 0L;
                            }
                        }

                        Boolean xmapRun = (Boolean) st.get("xmap_running");
                        if (Boolean.TRUE.equals(xmapRun)) continue;

                        Integer curMap = (Integer) st.get("map_id");
                        Integer curZone = (Integer) st.get("zone");

                        // Check if character is dead (let Goback handle healing & returning)
                        Class<?> clsF = getGameClass("nro.F");
                        if (clsF != null) {
                            Method mDead = getTypedMethod(clsF, "d", true, boolean.class);
                            Boolean isDead = (mDead != null) ? (Boolean) mDead.invoke(null) : Boolean.FALSE;
                            if (Boolean.TRUE.equals(isDead)) continue;
                        }

                        // 1. If target map is configured and we are not there, trigger Xmap travel (with 20s cooldown)
                        if (autoFarmMap >= 0 && (curMap == null || curMap.intValue() != autoFarmMap)) {
                            long now = System.currentTimeMillis();
                            if (now - lastWatchdogTravelMs > 20000L) {
                                lastWatchdogTravelMs = now;
                                System.out.println("[WATCHDOG] Character at map " + curMap + " (target=" + autoFarmMap + "). Triggering Xmap...");
                                travelToMap(autoFarmMap);
                            }
                            continue;
                        }

                        // 2. If at target map, verify target zone (only if autoFarmZone >= 0 was explicitly specified)
                        if (autoFarmZone >= 0 && curZone != null && curZone.intValue() != autoFarmZone) {
                            System.out.println("[WATCHDOG] Switching zone from " + curZone + " -> " + autoFarmZone);
                            executeModAction("k" + autoFarmZone);
                            Thread.sleep(800);
                        }

                        // 3. Enforce exact farming mode (COMBO, Chỉ TDLT, or Chỉ Tàn Sát)
                        applyFarmModeFlags(autoFarmMode);
                    } catch (Throwable ignored) {}
                }
            }
        }, "NRO-AutoFarm-Watchdog");
        wd.setDaemon(true);
        wd.start();
    }

    // Scan an item array (bag or body) for SKH items using type-safe reflection
    private static void scanItemsForSkh(Object[] items, String locationLabel, List<String> skhList) {
        if (items == null) return;
        try {
            Class<?> clsCD = getGameClass("nro.cD");
            Method isSkhMeth = (clsCD != null) ? getTypedMethod(clsCD, "b", true, boolean.class, clsCD) : null;

            for (int i = 0; i < items.length; i++) {
                Object item = items[i];
                if (item == null) continue;
                Field fTpl = getTypedFieldByTypeName(item.getClass(), "a", "nro.cE");
                Object template = (fTpl != null) ? fTpl.get(item) : null;
                if (template == null) continue;

                boolean isSkh = false;
                if (isSkhMeth != null) {
                    isSkh = Boolean.TRUE.equals(isSkhMeth.invoke(null, item));
                }

                Field fItemName = getTypedField(template.getClass(), "b", String.class);
                String itemName = (fItemName != null) ? String.valueOf(fItemName.get(template)) : "Item";

                Field fOpts = getTypedFieldByTypeName(item.getClass(), "b", "nro.cF[]");
                Object rawOpts = (fOpts != null) ? fOpts.get(item) : null;
                StringBuilder skhDesc = new StringBuilder();

                if (rawOpts instanceof Object[]) {
                    Object[] opts = (Object[]) rawOpts;
                    for (int j = 0; j < opts.length; j++) {
                        Object opt = opts[j];
                        if (opt == null) continue;
                        Field fOptTpl = getTypedFieldByTypeName(opt.getClass(), "a", "nro.cG");
                        Object optTpl = (fOptTpl != null) ? fOptTpl.get(opt) : null;
                        if (optTpl == null) continue;
                        Field fHq = getTypedField(optTpl.getClass(), "hQ", int.class);
                        int optId = (fHq != null) ? fHq.getInt(optTpl) : -1;
                        if (optId >= 127 && optId <= 144) {
                            isSkh = true;
                            try {
                                Method mE = getTypedMethod(opt.getClass(), "e", false, String.class);
                                Object formatted = (mE != null) ? mE.invoke(opt) : null;
                                if (formatted != null) {
                                    if (skhDesc.length() > 0) skhDesc.append(", ");
                                    skhDesc.append(formatted.toString());
                                }
                            } catch (Throwable ignored) {
                                Field fOptName = getTypedField(optTpl.getClass(), "b", String.class);
                                Object rawOptName = (fOptName != null) ? fOptName.get(optTpl) : null;
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

    // Get current game status & live character telemetry using type-safe field & method reflection
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
                Field fE = getTypedField(cls_f, "e", boolean.class);
                Field fL = getTypedField(cls_f, "l", int.class);
                if (fE != null) status.put("xmap_running", fE.getBoolean(null));
                if (fL != null) status.put("target_map", fL.getInt(null));
            }
        } catch (Throwable ignored) {}

        int currentMapId = -1;
        int currentZoneId = -1;
        try {
            Class<?> cls_av = getGameClass("nro.av");
            if (cls_av != null) {
                Field fH = getTypedField(cls_av, "h", int.class);
                Field fJ = getTypedField(cls_av, "j", int.class);
                Field fG = getTypedField(cls_av, "g", String[].class);
                if (fH != null) currentMapId = fH.getInt(null);
                if (fJ != null) currentZoneId = fJ.getInt(null);
                String[] mapNames = (fG != null) ? (String[]) fG.get(null) : null;
                String currentMapName = (mapNames != null && currentMapId >= 0 && currentMapId < mapNames.length) ? mapNames[currentMapId] : "Unknown";
                status.put("map_id", currentMapId);
                status.put("map_name", currentMapName);
                status.put("zone", currentZoneId);
            }
        } catch (Throwable ignored) {}

        boolean inGame = false;
        try {
            Object charObj = getMyChar();
            if (charObj != null) {
                Class<?> cCls = charObj.getClass();
                Field fGu = getTypedField(cCls, "gu", String.class);
                Field fAn = getTypedField(cCls, "aN", long.class);
                Field fAz = getTypedField(cCls, "az", long.class);
                Field fAx = getTypedField(cCls, "ax", long.class);
                Field fAs = getTypedField(cCls, "as", long.class);
                Field fAq = getTypedField(cCls, "aQ", long.class);
                Field fAo = getTypedField(cCls, "aO", long.class);
                Field fAw = getTypedField(cCls, "aw", long.class);
                Field fAv = getTypedField(cCls, "av", long.class);
                Field fBq = getTypedField(cCls, "bQ", boolean.class);
                Field fGv = getTypedField(cCls, "gv", int.class);
                Field fGa = getTypedField(cCls, "gA", int.class);

                String charName = (fGu != null) ? (String) fGu.get(charObj) : "";
                long hp = (fAn != null) ? fAn.getLong(charObj) : 0L;
                long hpMax = (fAz != null) ? fAz.getLong(charObj) : 0L;
                long mp = (fAx != null) ? fAx.getLong(charObj) : 0L;
                long mpMax = (fAs != null) ? fAs.getLong(charObj) : 0L;
                long power = (fAq != null) ? fAq.getLong(charObj) : 0L;
                long potential = (fAo != null) ? fAo.getLong(charObj) : 0L;
                long damage = (fAw != null) ? fAw.getLong(charObj) : 0L;
                long gold = (fAv != null) ? fAv.getLong(charObj) : 0L;
                boolean hasPet = (fBq != null) && fBq.getBoolean(charObj);
                int cx = (fGv != null) ? fGv.getInt(charObj) : 0;
                int cy = (fGa != null) ? fGa.getInt(charObj) : 0;

                inGame = (charName != null && !charName.trim().isEmpty() && currentMapId >= 0 && hpMax > 0);
                status.put("char_name", charName != null ? charName : "");
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
                        Object petObj = getMyPet();
                        if (petObj != null) {
                            Field fPetAq = getTypedField(petObj.getClass(), "aQ", long.class);
                            Field fPetAo = getTypedField(petObj.getClass(), "aO", long.class);
                            if (fPetAq != null) status.put("pet_power", fPetAq.getLong(petObj));
                            if (fPetAo != null) status.put("pet_potential", fPetAo.getLong(petObj));
                        }
                    } catch (Throwable ignored) {}
                }

                // Scan bag and body for SKH items and slot usage
                List<String> skhList = new ArrayList<String>();
                Field fBody = getTypedFieldByTypeName(cCls, "a", "nro.cD[]");
                Object rawBody = (fBody != null) ? fBody.get(charObj) : null;
                if (rawBody instanceof Object[]) {
                    scanItemsForSkh((Object[]) rawBody, "Đang mặc", skhList);
                }
                Field fBag = getTypedFieldByTypeName(cCls, "b", "nro.cD[]");
                Object rawBag = (fBag != null) ? fBag.get(charObj) : null;
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
        } catch (Throwable t) {
            System.out.println("[BRIDGE] getStatus char telemetry error: " + t);
        }

        status.put("in_game", inGame);

        // Real-time mod boolean flags
        String[] trackedFlags = new String[]{"ts", "tdlt", "gb", "anhat", "locdo", "xdh", "annguoi", "alogin", "sz", "kvt", "nb", "cflag"};
        for (int i = 0; i < trackedFlags.length; i++) {
            String fName = trackedFlags[i];
            Boolean val = getModFlag(fName);
            status.put("mod_" + fName, Boolean.TRUE.equals(val));
        }

        // Also expose in-game TDLT status (nro.ct.cT) and remaining TDLT minutes (nro.p.b())
        try {
            Class<?> clsCt = getGameClass("nro.ct");
            if (clsCt != null) {
                Field fCt = getTypedField(clsCt, "cT", boolean.class);
                if (fCt != null) status.put("ingame_tdlt", fCt.getBoolean(null));
            }
            Class<?> clsP = getGameClass("nro.p");
            if (clsP != null) {
                Method mB = getTypedMethod(clsP, "b", true, int.class);
                Object mins = (mB != null) ? mB.invoke(null) : null;
                if (mins instanceof Number) status.put("tdlt_minutes", ((Number) mins).intValue());
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
            } else if (path.equals("/api/game/anti_stuck")) {
                stopXmap();
                try {
                    Class<?> clsQ = getGameClass("nro.Q");
                    if (clsQ != null) {
                        Method mBo = getTypedMethod(clsQ, "bo", true, void.class);
                        if (mBo != null) mBo.invoke(null);
                    }
                } catch (Throwable ignored) {}
                jsonResp = "{\"status\": \"ok\"}";
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
                if (skillNumber <= 0) skillNumber = 1;
                jsonResp = "{\"status\": \"ok\", \"skill\": " + skillNumber + "}";
            } else if (path.equals("/api/game/auto_farm")) {
                // Batch auto farm sequence with smart map travel & persistent 24/7 state
                final String mapStr = params.get("map");
                final String zoneStr = params.get("zone");
                final String mode = params.get("mode") != null ? params.get("mode") : "all";

                int parsedMap = -1;
                if (mapStr != null) {
                    String ms = mapStr.trim();
                    if (!ms.isEmpty() && !ms.equalsIgnoreCase("xmap") && !ms.equalsIgnoreCase("keep") && !ms.equals("-1")) {
                        try { parsedMap = Integer.parseInt(ms); } catch (Throwable ignored) {}
                    }
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

                // Configure punch skill immediately
                configurePunchSkill();

                // Check if we need to travel to another map first
                Map<String, Object> curStat = getStatus();
                final Integer initialCurMap = (Integer) curStat.get("map_id");
                final boolean needMapTravel = (autoFarmMap >= 0 && (initialCurMap == null || initialCurMap.intValue() != autoFarmMap));

                setModFlag("alogin", true);
                setModFlag("sz", true);
                setModFlag("checklag", true);

                if (needMapTravel) {
                    // Pause conflicting auto loops and start Xmap immediately on this request
                    lastWatchdogTravelMs = System.currentTimeMillis();
                    travelToMap(autoFarmMap);
                } else if (autoFarmZone < 0) {
                    // Already at target map (or keeping current map) and no zone switch needed: apply flags immediately
                    applyFarmModeFlags(mode);
                    setModFlag("locdo", true);
                    setModFlag("anhat", true);
                    executeModAction("s20");
                }

                farmThread = new Thread(new Runnable() {
                    public void run() {
                        try {
                            // 1. If target map specified and not there yet, monitor Xmap travel until arrival
                            if (needMapTravel) {
                                System.out.println("[BRIDGE] AutoFarm: Traveling from map " + initialCurMap + " to map " + autoFarmMap + "...");
                                long startWait = System.currentTimeMillis();
                                boolean arrived = false;
                                int retryCount = 0;
                                Integer lastProgressMap = initialCurMap;

                                while (System.currentTimeMillis() - startWait < 60000L) {
                                    Thread.sleep(600);
                                    // Keep in-game TDLT (nro.ct.cT) off while traveling across maps
                                    try {
                                        Class<?> clsCt = getGameClass("nro.ct");
                                        if (clsCt != null) {
                                            Field fCt = getTypedField(clsCt, "cT", boolean.class);
                                            if (fCt != null && fCt.getBoolean(null)) {
                                                fCt.setBoolean(null, false);
                                            }
                                        }
                                    } catch (Throwable ignored) {}

                                    Map<String, Object> st = getStatus();
                                    Integer cm = (Integer) st.get("map_id");
                                    Boolean xmapRun = (Boolean) st.get("xmap_running");

                                    if (cm != null && cm.intValue() == autoFarmMap) {
                                        if (Boolean.TRUE.equals(xmapRun)) {
                                            stopXmap();
                                        }
                                        System.out.println("[BRIDGE] AutoFarm: Arrived at target map " + autoFarmMap + "!");
                                        arrived = true;
                                        break;
                                    }

                                    if (cm != null && (lastProgressMap == null || cm.intValue() != lastProgressMap.intValue())) {
                                        lastProgressMap = cm;
                                        retryCount = 0;
                                    }

                                    // If Xmap stopped before reaching autoFarmMap, retry up to 2 times
                                    if (!Boolean.TRUE.equals(xmapRun)) {
                                        if (retryCount < 2) {
                                            retryCount++;
                                            System.out.println("[BRIDGE] AutoFarm: Xmap paused at map " + cm + ", retrying travel to " + autoFarmMap + " (" + retryCount + "/2)...");
                                            lastWatchdogTravelMs = System.currentTimeMillis();
                                            travelToMap(autoFarmMap);
                                            Thread.sleep(1000);
                                        } else {
                                            System.out.println("[BRIDGE] AutoFarm: Map " + autoFarmMap + " unreachable from map " + cm + "; farming at current map " + cm);
                                            if (cm != null && cm.intValue() >= 0) {
                                                autoFarmMap = cm.intValue();
                                                saveAutoFarmState();
                                            }
                                            break;
                                        }
                                    }
                                }
                                if (arrived) {
                                    Thread.sleep(600);
                                }
                            }

                            // 2. Switch zone if explicitly specified
                            if (autoFarmZone >= 0) {
                                executeModAction("k" + autoFarmZone);
                                Thread.sleep(600);
                            }

                            // 3. Configure melee skill and apply exact farming mode
                            configurePunchSkill();
                            applyFarmModeFlags(mode);

                            // 4. Enable Lọc Đồ + Auto Nhặt + Speed
                            setModFlag("locdo", true);
                            setModFlag("anhat", true);
                            executeModAction("s20");

                            System.out.println("[BRIDGE] AutoFarm 24/7 mode [" + mode + "] active (map=" + autoFarmMap + ", zone=" + autoFarmZone + ")");
                        } catch (InterruptedException ie) {
                            System.out.println("[BRIDGE] AutoFarm thread interrupted.");
                        } catch (Throwable t) {
                            System.out.println("[BRIDGE] AutoFarm error: " + t);
                        }
                    }
                }, "AutoFarm-Worker");
                farmThread.setDaemon(true);
                farmThread.start();

                jsonResp = "{\"status\": \"ok\", \"message\": \"Auto farm started\", \"map\": \"" + autoFarmMap + "\", \"traveling\": " + needMapTravel + ", \"zone\": \"" + zoneStr + "\", \"mode\": \"" + mode + "\"}";
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
                setModFlag("kvt", false);
                setModFlag("ak", false);
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
