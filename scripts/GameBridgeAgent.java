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
import java.util.concurrent.*;

public class GameBridgeAgent implements Runnable, ThreadFactory {
    private static Instrumentation instrumentation;
    private static int serverPort = 28888;
    private static volatile boolean running = true;
    private static volatile Thread serverThread = null;
    private static volatile ExecutorService httpExecutor = null;
    private static volatile int workerCounter = 0;

    private static final int TASK_HTTP_SERVER = 1;
    private static final int TASK_HANDLE_CLIENT = 2;
    private static final int TASK_WATCHDOG = 3;
    private static final int TASK_DIRECT_LOGIN = 4;
    private static final int TASK_AUTO_FARM = 5;

    private final int taskType;
    private final Socket clientSocket;
    private final String loginUser;
    private final String loginPass;
    private final int loginServer;
    private final boolean farmNeedTravel;
    private final Integer farmInitialMap;
    private final int farmTargetMap;
    private final String farmMode;

    public GameBridgeAgent() {
        this(0, null, null, null, 0, false, null, -1, null);
    }

    private GameBridgeAgent(int taskType, Socket clientSocket, String loginUser, String loginPass, int loginServer,
                            boolean farmNeedTravel, Integer farmInitialMap, int farmTargetMap, String farmMode) {
        this.taskType = taskType;
        this.clientSocket = clientSocket;
        this.loginUser = loginUser;
        this.loginPass = loginPass;
        this.loginServer = loginServer;
        this.farmNeedTravel = farmNeedTravel;
        this.farmInitialMap = farmInitialMap;
        this.farmTargetMap = farmTargetMap;
        this.farmMode = farmMode;
    }

    public Thread newThread(Runnable r) {
        Thread t = new Thread(r, "NRO-Bridge-Worker-" + (++workerCounter));
        t.setDaemon(true);
        return t;
    }

    private static synchronized ExecutorService getHttpExecutor() {
        if (httpExecutor == null) {
            httpExecutor = Executors.newFixedThreadPool(8, new GameBridgeAgent());
        }
        return httpExecutor;
    }

    public void run() {
        if (taskType == TASK_HTTP_SERVER) {
            runHttpServerLoop();
        } else if (taskType == TASK_HANDLE_CLIENT) {
            handleClient(clientSocket);
        } else if (taskType == TASK_WATCHDOG) {
            runWatchdogLoop();
        } else if (taskType == TASK_DIRECT_LOGIN) {
            runDirectLoginWorker(loginUser, loginPass, loginServer);
        } else if (taskType == TASK_AUTO_FARM) {
            runAutoFarmWorker(farmNeedTravel, farmInitialMap, farmTargetMap, farmMode);
        }
    }

    // Keep track of background farm task & persistent 24/7 state
    private static Thread farmThread = null;
    private static volatile boolean autoFarmActive = false;
    private static volatile int autoFarmMap = -1;
    private static volatile int autoFarmZone = -1;
    private static volatile String autoFarmMode = "all";
    private static volatile long lastWatchdogTravelMs = 0L;
    private static volatile long buyingTdltSinceMs = 0L;
    private static volatile long lastTdltBuyAttemptMs = 0L;

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

    public static void dismissDialog() {
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            if (clsCr != null) {
                Method mcL = getTypedMethod(clsCr, "cL", true, void.class);
                if (mcL != null) mcL.invoke(null);
            }
        } catch (Throwable ignored) {}
    }

    // Check if the game MIDlet (nro.hero68) has started and left MicroEmulator's Launcher screen
    public static boolean isMidletLoaded() {
        try {
            Class<?> mbCls = Class.forName("org.microemu.MIDletBridge", false, ClassLoader.getSystemClassLoader());
            Method mGetCur = mbCls.getMethod("getCurrentMIDlet");
            Object cur = mGetCur.invoke(null);
            if (cur != null && !cur.getClass().getName().endsWith("Launcher")) {
                return true;
            }
        } catch (Throwable ignored) {}
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            if (clsCr != null) {
                Field fA = getTypedFieldByTypeName(clsCr, "a", "nro.cr");
                if (fA != null && fA.get(null) != null) return true;
            }
        } catch (Throwable ignored) {}
        return false;
    }

    private static volatile long lastLaunchAttemptMs = 0L;

    // Automatically launch the selected MIDlet if MicroEmulator has finished booting into the Launcher screen
    public static synchronized boolean autoLaunchMidletIfNeeded() {
        try {
            if (isMidletLoaded()) return true;
            if (!isDeviceReady()) return false;
            long now = System.currentTimeMillis();
            if (now - lastLaunchAttemptMs < 2500L) return false;

            Class<?> mbCls = Class.forName("org.microemu.MIDletBridge", false, ClassLoader.getSystemClassLoader());
            Method mGetCur = mbCls.getMethod("getCurrentMIDlet");
            Object cur = mGetCur.invoke(null);
            if (cur != null && cur.getClass().getName().endsWith("Launcher")) {
                Field fMenu = cur.getClass().getDeclaredField("menuList");
                fMenu.setAccessible(true);
                Object menuList = fMenu.get(cur);
                if (menuList == null) return false;

                Method mGetEntry = cur.getClass().getMethod("getSelectedMidletEntry");
                Object entry = mGetEntry.invoke(cur);
                if (entry == null) return false;

                Field fCommon = cur.getClass().getDeclaredField("common");
                fCommon.setAccessible(true);
                Object common = fCommon.get(cur);
                if (common == null) return false;

                // Ensure Main.main() has completely finished initMIDlet(false) (which clears midletClassOrUrl to null)
                try {
                    Field fUrl = common.getClass().getDeclaredField("midletClassOrUrl");
                    fUrl.setAccessible(true);
                    if (fUrl.get(common) != null) {
                        return false;
                    }
                } catch (Throwable ignored) {}

                lastLaunchAttemptMs = now;
                Method mInit = common.getClass().getMethod("initMIDlet", boolean.class);
                mInit.setAccessible(true);
                mInit.invoke(common, Boolean.TRUE);
                System.out.println("[BRIDGE] Auto-launched MIDlet from MicroEmulator Launcher!");
                return true;
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] autoLaunchMidletIfNeeded note: " + t);
        }
        return false;
    }

    private static Object getDisplayAccess() {
        try {
            Class<?> mbCls = Class.forName("org.microemu.MIDletBridge", false, ClassLoader.getSystemClassLoader());
            Method mGetAccess = mbCls.getMethod("getMIDletAccess");
            Object ma = mGetAccess.invoke(null);
            if (ma == null) return null;
            Method mGetDisp = ma.getClass().getMethod("getDisplayAccess");
            mGetDisp.setAccessible(true);
            return mGetDisp.invoke(ma);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static void repaintDisplay() {
        try {
            Object da = getDisplayAccess();
            if (da != null) {
                Method mRepaint = da.getClass().getMethod("repaint");
                mRepaint.setAccessible(true);
                mRepaint.invoke(da);
            }
        } catch (Throwable ignored) {}
        try {
            if (cachedCanvasComp != null) {
                cachedCanvasComp.repaint();
            }
        } catch (Throwable ignored) {}
    }

    public static int mapKeyToJ2meCode(String rawKey) {
        if (rawKey == null) return 0;
        String k = rawKey.trim();
        String lower = k.toLowerCase(Locale.ROOT);
        if ("up".equals(lower) || "arrowup".equals(lower)) return -1;
        if ("down".equals(lower) || "arrowdown".equals(lower)) return -2;
        if ("left".equals(lower) || "arrowleft".equals(lower)) return -3;
        if ("right".equals(lower) || "arrowright".equals(lower)) return -4;
        if ("ok".equals(lower) || "select".equals(lower) || "return".equals(lower) || "enter".equals(lower) || "kp_enter".equals(lower)) return -5;
        if ("f1".equals(lower) || "soft1".equals(lower) || "escape".equals(lower)) return -6;
        if ("f2".equals(lower) || "soft2".equals(lower)) return -7;
        if ("backspace".equals(lower) || "clear".equals(lower) || "delete".equals(lower)) return -8;
        if ("star".equals(lower) || "asterisk".equals(lower) || "*".equals(k)) return 42;
        if ("hash".equals(lower) || "numbersign".equals(lower) || "pound".equals(lower) || "#".equals(k)) return 35;
        if ("space".equals(lower) || "kp_space".equals(lower) || " ".equals(k)) return 32;
        if ("tab".equals(lower)) return 9;
        if (k.length() == 1) {
            return (int) k.charAt(0);
        }
        return 0;
    }

    // Dispatch a J2ME key press & release directly into MicroEmulator / DragonBoy Canvas
    public static boolean sendKey(String rawKey) {
        try {
            int keyCode = mapKeyToJ2meCode(rawKey);
            if (keyCode == 0) return false;

            if (!isMidletLoaded() && (keyCode == -5 || keyCode == -7 || keyCode == 32 || keyCode == 53)) {
                if (autoLaunchMidletIfNeeded()) return true;
            }

            boolean delivered = false;
            Object da = getDisplayAccess();
            if (da != null) {
                Method mPress = da.getClass().getMethod("keyPressed", int.class);
                Method mRelease = da.getClass().getMethod("keyReleased", int.class);
                mPress.setAccessible(true);
                mRelease.setAccessible(true);
                mPress.invoke(da, Integer.valueOf(keyCode));
                Thread.sleep(35);
                mRelease.invoke(da, Integer.valueOf(keyCode));
                delivered = true;
            }

            if (!delivered) {
                Class<?> clsCr = getGameClass("nro.cr");
                if (clsCr != null) {
                    Field fCanvas = getTypedFieldByTypeName(clsCr, "a", "nro.cr");
                    Object canvas = (fCanvas != null) ? fCanvas.get(null) : null;
                    if (canvas != null) {
                        Method mPress = getTypedMethod(clsCr, "keyPressed", false, void.class, int.class);
                        Method mRelease = getTypedMethod(clsCr, "keyReleased", false, void.class, int.class);
                        if (mPress != null) mPress.invoke(canvas, Integer.valueOf(keyCode));
                        Thread.sleep(35);
                        if (mRelease != null) mRelease.invoke(canvas, Integer.valueOf(keyCode));
                        delivered = true;
                    }
                }
            }

            repaintDisplay();
            return delivered;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] sendKey error for [" + rawKey + "]: " + t);
            return false;
        }
    }

    // Dispatch a 240x360 pointer touch/click directly into MicroEmulator / DragonBoy Canvas
    public static boolean sendPointerClick(int x, int y) {
        try {
            int cx = Math.max(0, Math.min(239, x));
            int cy = Math.max(0, Math.min(359, y));

            if (!isMidletLoaded()) {
                if (autoLaunchMidletIfNeeded()) return true;
            }

            boolean delivered = false;
            Object da = getDisplayAccess();
            if (da != null) {
                Method mPress = da.getClass().getMethod("pointerPressed", int.class, int.class);
                Method mRelease = da.getClass().getMethod("pointerReleased", int.class, int.class);
                mPress.setAccessible(true);
                mRelease.setAccessible(true);
                mPress.invoke(da, Integer.valueOf(cx), Integer.valueOf(cy));
                Thread.sleep(40);
                mRelease.invoke(da, Integer.valueOf(cx), Integer.valueOf(cy));
                delivered = true;
            }

            if (!delivered) {
                Class<?> clsCr = getGameClass("nro.cr");
                if (clsCr != null) {
                    Field fCanvas = getTypedFieldByTypeName(clsCr, "a", "nro.cr");
                    Object canvas = (fCanvas != null) ? fCanvas.get(null) : null;
                    if (canvas != null) {
                        Method mPress = getTypedMethod(clsCr, "pointerPressed", false, void.class, int.class, int.class);
                        Method mRelease = getTypedMethod(clsCr, "pointerReleased", false, void.class, int.class, int.class);
                        if (mPress != null) mPress.invoke(canvas, Integer.valueOf(cx), Integer.valueOf(cy));
                        Thread.sleep(40);
                        if (mRelease != null) mRelease.invoke(canvas, Integer.valueOf(cx), Integer.valueOf(cy));
                        delivered = true;
                    }
                }
            }

            repaintDisplay();
            return delivered;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] sendPointerClick error (" + x + "," + y + "): " + t);
            return false;
        }
    }

    // Locate the currently focused or target TField (nro.au) in DragonBoy
    private static Object findTargetTField(String fieldHint) {
        try {
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCJ = getGameClass("nro.cJ");
            Class<?> clsCk = getGameClass("nro.ck");
            Class<?> clsBR = getGameClass("nro.bR");
            Class<?> clsAu = getGameClass("nro.au");
            if (clsCr == null || clsAu == null) return null;

            String hint = (fieldHint != null) ? fieldHint.trim().toLowerCase(Locale.ROOT) : "";

            // 1. Explicit hint for LoginScr user / pass
            if (("user".equals(hint) || "pass".equals(hint)) && clsCJ != null) {
                Field fLoginScr = getTypedFieldByTypeName(clsCr, "a", "nro.cJ");
                Object loginScr = (fLoginScr != null) ? fLoginScr.get(null) : null;
                if (loginScr != null) {
                    Field fUser = getTypedFieldByTypeName(clsCJ, "b", "nro.au");
                    Field fPass = getTypedFieldByTypeName(clsCJ, "e", "nro.au");
                    Object tfUser = (fUser != null) ? fUser.get(loginScr) : null;
                    Object tfPass = (fPass != null) ? fPass.get(loginScr) : null;
                    Field fFocus = getTypedField(clsAu, "bf", boolean.class);
                    if ("user".equals(hint) && tfUser != null) {
                        if (fFocus != null) {
                            fFocus.setBoolean(tfUser, true);
                            if (tfPass != null) fFocus.setBoolean(tfPass, false);
                        }
                        return tfUser;
                    }
                    if ("pass".equals(hint) && tfPass != null) {
                        if (fFocus != null) {
                            fFocus.setBoolean(tfPass, true);
                            if (tfUser != null) fFocus.setBoolean(tfUser, false);
                        }
                        return tfPass;
                    }
                }
            }

            // 2. Check if InputDlg (nro.cr.a of type nro.ck) is currently open as active dialog (nro.cr.a of type nro.Z)
            if (clsCk != null) {
                Field fInputDlg = getTypedFieldByTypeName(clsCr, "a", "nro.ck");
                Field fCurDlg = getTypedFieldByTypeName(clsCr, "a", "nro.Z");
                Object inputDlg = (fInputDlg != null) ? fInputDlg.get(null) : null;
                Object curDlg = (fCurDlg != null) ? fCurDlg.get(null) : null;
                if (inputDlg != null && inputDlg == curDlg) {
                    Field fTf = getTypedFieldByTypeName(clsCk, "a", "nro.au");
                    if (fTf != null) {
                        Object tf = fTf.get(inputDlg);
                        if (tf != null) return tf;
                    }
                }
            }

            // 3. Check if ChatTextField (nro.bR) is open
            if (clsBR != null) {
                Field fChat = getTypedFieldByTypeName(clsCr, "a", "nro.bR");
                Object chatInst = (fChat != null) ? fChat.get(null) : null;
                if (chatInst == null) {
                    Field fStaticChat = getTypedFieldByTypeName(clsBR, "a", "nro.bR");
                    if (fStaticChat != null) chatInst = fStaticChat.get(null);
                }
                if (chatInst != null) {
                    Field fShow = getTypedField(clsBR, "bf", boolean.class);
                    if (fShow != null && fShow.getBoolean(chatInst)) {
                        Field fTf = getTypedFieldByTypeName(clsBR, "a", "nro.au");
                        if (fTf != null) {
                            Object tf = fTf.get(chatInst);
                            if (tf != null) return tf;
                        }
                    }
                }
            }

            // 4. Check LoginScr (nro.cJ) TFields (b = user, e = pass)
            if (clsCJ != null) {
                Field fLoginScr = getTypedFieldByTypeName(clsCr, "a", "nro.cJ");
                Object loginScr = (fLoginScr != null) ? fLoginScr.get(null) : null;
                if (loginScr != null) {
                    Field fUser = getTypedFieldByTypeName(clsCJ, "b", "nro.au");
                    Field fPass = getTypedFieldByTypeName(clsCJ, "e", "nro.au");
                    Object tfUser = (fUser != null) ? fUser.get(loginScr) : null;
                    Object tfPass = (fPass != null) ? fPass.get(loginScr) : null;
                    Field fFocus = getTypedField(clsAu, "bf", boolean.class);
                    if (fFocus != null) {
                        if (tfPass != null && fFocus.getBoolean(tfPass)) return tfPass;
                        if (tfUser != null && fFocus.getBoolean(tfUser)) return tfUser;
                    }
                    if (tfUser != null) return tfUser;
                }
            }
        } catch (Throwable ignored) {}
        return null;
    }

    // Set text directly into the active J2ME InputMethod or DragonBoy TField (nro.au)
    public static boolean sendTextInput(String text, String fieldHint, String submit) {
        if (text == null) text = "";
        boolean handled = false;
        try {
            // 1. Try setting DragonBoy's TField (nro.au) directly
            Object tf = findTargetTField(fieldHint);
            if (tf != null) {
                Method mSetText = getTypedMethod(tf.getClass(), "t", false, void.class, String.class);
                if (mSetText != null) {
                    mSetText.invoke(tf, text);
                    handled = true;
                }
            }

            // 2. Also try MicroEmulator's J2SEInputMethod.clipboardPaste if a native LCDUI TextBox is active
            try {
                Class<?> dfCls = Class.forName("org.microemu.device.DeviceFactory", false, ClassLoader.getSystemClassLoader());
                Object dev = dfCls.getMethod("getDevice").invoke(null);
                if (dev != null) {
                    Object im = dev.getClass().getMethod("getInputMethod").invoke(dev);
                    if (im != null) {
                        Field fListener = im.getClass().getSuperclass().getDeclaredField("inputMethodListener");
                        fListener.setAccessible(true);
                        if (fListener.get(im) != null) {
                            Method mPaste = im.getClass().getMethod("clipboardPaste", String.class);
                            mPaste.invoke(im, text);
                            handled = true;
                        }
                    }
                }
            } catch (Throwable ignored) {}

            if (submit != null) {
                String sub = submit.trim().toLowerCase(Locale.ROOT);
                if ("f2".equals(sub)) {
                    Thread.sleep(40);
                    sendKey("f2");
                } else if ("enter".equals(sub) || "return".equals(sub) || "ok".equals(sub)) {
                    Thread.sleep(40);
                    sendKey("enter");
                }
            }
            repaintDisplay();
        } catch (Throwable t) {
            System.out.println("[BRIDGE] sendTextInput error: " + t);
        }
        return handled;
    }

    public static boolean clearActiveText() {
        boolean handled = false;
        try {
            Object tf = findTargetTField(null);
            if (tf != null) {
                Method mSetText = getTypedMethod(tf.getClass(), "t", false, void.class, String.class);
                if (mSetText != null) {
                    mSetText.invoke(tf, "");
                    handled = true;
                }
                Method mClear = getTypedMethod(tf.getClass(), "eh", false, void.class);
                if (mClear != null) {
                    mClear.invoke(tf);
                    handled = true;
                }
            }
            sendKey("backspace");
            repaintDisplay();
        } catch (Throwable t) {
            System.out.println("[BRIDGE] clearActiveText error: " + t);
        }
        return handled;
    }

    // Open the Account / Login screen (nro.cJ) directly inside JVM
    public static boolean openAccountDialog() {
        try {
            autoLaunchMidletIfNeeded();
            dismissDialog();
            Class<?> clsE = getGameClass("nro.e");
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCJ = getGameClass("nro.cJ");
            Class<?> clsAu = getGameClass("nro.au");
            if (clsE != null) {
                Method mH = getTypedMethod(clsE, "h", true, void.class);
                if (mH != null) mH.invoke(null);
            }
            if (clsCr != null && clsCJ != null) {
                Field fLoginScr = getTypedFieldByTypeName(clsCr, "a", "nro.cJ");
                if (fLoginScr != null) {
                    Object loginScr = fLoginScr.get(null);
                    if (loginScr == null) {
                        loginScr = clsCJ.getDeclaredConstructor().newInstance();
                        fLoginScr.set(null, loginScr);
                    }
                    Method mCr = getTypedMethod(clsCJ, "cR", false, void.class);
                    if (mCr != null) mCr.invoke(loginScr);

                    if (clsAu != null) {
                        Field fUser = getTypedFieldByTypeName(clsCJ, "b", "nro.au");
                        Field fPass = getTypedFieldByTypeName(clsCJ, "e", "nro.au");
                        Field fFocus = getTypedField(clsAu, "bf", boolean.class);
                        if (fFocus != null) {
                            Object tfUser = (fUser != null) ? fUser.get(loginScr) : null;
                            Object tfPass = (fPass != null) ? fPass.get(loginScr) : null;
                            if (tfUser != null) fFocus.setBoolean(tfUser, true);
                            if (tfPass != null) fFocus.setBoolean(tfPass, false);
                        }
                    }
                    repaintDisplay();
                    return true;
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] openAccountDialog error: " + t);
        }
        return false;
    }

    public static void premain(String agentArgs, Instrumentation inst) {
        try {
            instrumentation = inst;
            System.out.println("[BRIDGE] GameBridgeAgent premain loaded!");
            loadAutoFarmState();
            startHttpServer();
            startWatchdogThread();
        } catch (Throwable t) {
            System.out.println("[BRIDGE] premain critical error: " + t);
            t.printStackTrace();
        }
    }

    public static void agentmain(String agentArgs, Instrumentation inst) {
        try {
            instrumentation = inst;
            System.out.println("[BRIDGE] GameBridgeAgent agentmain loaded!");
            loadAutoFarmState();
            startHttpServer();
            startWatchdogThread();
        } catch (Throwable t) {
            System.out.println("[BRIDGE] agentmain critical error: " + t);
            t.printStackTrace();
        }
    }

    private static volatile ClassLoader cachedClassLoader = null;

    private static ClassLoader findMidletClassLoader() {
        try {
            Class<?> mbCls = Class.forName("org.microemu.MIDletBridge", false, ClassLoader.getSystemClassLoader());
            Method mGetCur = mbCls.getMethod("getCurrentMIDlet");
            Object cur = mGetCur.invoke(null);
            if (cur != null && !cur.getClass().getName().endsWith("Launcher")) {
                ClassLoader activeCl = cur.getClass().getClassLoader();
                if (activeCl != null) {
                    cachedClassLoader = activeCl;
                    return activeCl;
                }
            }
        } catch (Throwable ignored) {}

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

    private static boolean isDeviceReady() {
        try {
            Class<?> dfCls = Class.forName("org.microemu.device.DeviceFactory", false, ClassLoader.getSystemClassLoader());
            Method mGet = dfCls.getMethod("getDevice");
            Object dev = mGet.invoke(null);
            if (dev == null) return false;
            Method mDisp = dev.getClass().getMethod("getDeviceDisplay");
            mDisp.setAccessible(true);
            return mDisp.invoke(dev) != null;
        } catch (Throwable ignored) {
            return isMidletLoaded();
        }
    }

    private static void ensureModControllerInitialized() {
        try {
            Class<?> clsF = getGameClass("nro.F");
            if (clsF != null) {
                Method mFa = getTypedMethodByReturnName(clsF, "a", true, "nro.F");
                if (mFa != null) mFa.invoke(null);
            }
        } catch (Throwable ignored) {}
    }

    private static Class<?> getGameClass(String name) {
        if ("nro.F".equals(name) && !isDeviceReady()) {
            return null;
        }
        ClassLoader cl = findMidletClassLoader();
        if (cl != null) {
            try {
                return Class.forName(name, false, cl);
            } catch (Throwable ignored) {}
        }
        if (instrumentation != null) {
            for (Class<?> c : instrumentation.getAllLoadedClasses()) {
                if (c.getName().equals(name)) return c;
            }
        }
        try {
            return Class.forName(name, false, ClassLoader.getSystemClassLoader());
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

    // Safely retrieve myCharZ singleton (static field nro.cd m or static nro.cd b())
    private static Object getMyChar() {
        try {
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsCd == null) return null;
            Field fM = getTypedFieldByTypeName(clsCd, "m", "nro.cd");
            if (fM != null) {
                Object c = fM.get(null);
                if (c != null) return c;
            }
            Method mB = getTypedMethodByReturnName(clsCd, "b", true, "nro.cd");
            if (mB != null) {
                Object c = mB.invoke(null);
                if (c != null) return c;
            }
        } catch (Throwable ignored) {}
        return null;
    }

    // Safely retrieve myPet singleton (static field nro.cd l or static nro.cd c())
    private static Object getMyPet() {
        try {
            Class<?> clsCd = getGameClass("nro.cd");
            if (clsCd == null) return null;
            Field fL = getTypedFieldByTypeName(clsCd, "l", "nro.cd");
            if (fL != null) {
                Object p = fL.get(null);
                if (p != null) return p;
            }
            Method mC = getTypedMethodByReturnName(clsCd, "c", true, "nro.cd");
            if (mC != null) {
                Object p = mC.invoke(null);
                if (p != null) return p;
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
        if ("ats".equals(c)) return new String[]{"nro.j", "n"};
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

    // Returns int[]{slot, remainingMinutes} for item 521 (Tự động luyện tập) in character's bag
    private static int[] findTdltItemInBag() {
        int slot = -1;
        int itemMins = 0;
        try {
            Class<?> clsCD = getGameClass("nro.cD");
            if (clsCD != null) {
                Method mFindSlot = getTypedMethod(clsCD, "b", true, int.class, short.class);
                if (mFindSlot != null) {
                    Object sObj = mFindSlot.invoke(null, (short) 521);
                    if (sObj instanceof Number) slot = ((Number) sObj).intValue();
                }
                Method mFindItem = getTypedMethod(clsCD, "a", true, clsCD, short.class);
                Object itemObj = (mFindItem != null) ? mFindItem.invoke(null, (short) 521) : null;
                if (itemObj != null) {
                    Field fOpts = getTypedFieldByTypeName(itemObj.getClass(), "b", "nro.cF[]");
                    Object rawOpts = (fOpts != null) ? fOpts.get(itemObj) : null;
                    if (rawOpts instanceof Object[]) {
                        Object[] opts = (Object[]) rawOpts;
                        boolean foundTimeOpt = false;
                        for (int i = 0; i < opts.length; i++) {
                            Object opt = opts[i];
                            if (opt == null) continue;
                            Field fOptTpl = getTypedFieldByTypeName(opt.getClass(), "a", "nro.cG");
                            Object optTpl = (fOptTpl != null) ? fOptTpl.get(opt) : null;
                            if (optTpl == null) continue;
                            Field fHq = getTypedField(optTpl.getClass(), "hQ", int.class);
                            if (fHq != null && fHq.getInt(optTpl) == 1) {
                                foundTimeOpt = true;
                                Field fBa = getTypedField(opt.getClass(), "bA", int.class);
                                if (fBa != null) itemMins = fBa.getInt(opt);
                                break;
                            }
                        }
                        if (!foundTimeOpt) {
                            itemMins = 60;
                        }
                    } else {
                        // Options not populated on bag item yet; treat as usable
                        itemMins = 60;
                    }
                }
            }
        } catch (Throwable ignored) {}
        return new int[]{slot, itemMins};
    }

    // Set or clear in-game TDLT flag (nro.ct.cT), use TDLT item 521 from bag, or trigger auto-buy at Map 5 (Đảo Kame)
    private static void syncInGameTdltState(boolean enable) {
        try {
            Class<?> clsCt = getGameClass("nro.ct");
            Class<?> clsP = getGameClass("nro.p");
            Class<?> clsF = getGameClass("nro.F");
            Class<?> clsA = getGameClass("nro.A");

            if (!enable) {
                if (clsP != null) {
                    Field fZ = getTypedField(clsP, "z", boolean.class);
                    if (fZ != null) fZ.setBoolean(null, false);
                    Field fB = getTypedField(clsP, "B", boolean.class);
                    if (fB != null) fB.setBoolean(null, false);
                }
                if (clsCt != null) {
                    Field fCt = getTypedField(clsCt, "cT", boolean.class);
                    if (fCt != null) fCt.setBoolean(null, false);
                }
                return;
            }

            // enable == true: Ensure mod controller is initialized, keep nro.p.z = true, nro.ct.cT = true, and Goback enabled
            ensureModControllerInitialized();
            configurePunchSkill();
            if (clsP != null) {
                Field fZ = getTypedField(clsP, "z", boolean.class);
                if (fZ != null) fZ.setBoolean(null, true);
                Field fB = getTypedField(clsP, "B", boolean.class);
                if (fB != null) fB.setBoolean(null, false);
            }
            if (clsCt != null) {
                Field fCt = getTypedField(clsCt, "cT", boolean.class);
                if (fCt != null) fCt.setBoolean(null, true);
            }
            if (clsA != null) {
                Field fJ = getTypedField(clsA, "J", boolean.class);
                if (fJ != null) fJ.setBoolean(null, true);
                updateGobackTarget(autoFarmMap, autoFarmZone);
            }

            // Ensure nro.F.aL >= 1 (auto-renew threshold) and nro.F.aQ >= 1 (Santa purchase quantity)
            if (clsF != null) {
                Field fAl = getTypedField(clsF, "aL", int.class);
                if (fAl != null && fAl.getInt(null) < 1) {
                    fAl.setInt(null, 1);
                }
                Field fAq = getTypedField(clsF, "aQ", int.class);
                if (fAq != null && fAq.getInt(null) < 1) {
                    fAq.setInt(null, 1);
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

            // Case 1: Active TDLT buff already running (tdltMinutes > 0)
            if (tdltMinutes > 0) {
                if (clsCt != null) {
                    Field fCt = getTypedField(clsCt, "cT", boolean.class);
                    if (fCt != null) fCt.setBoolean(null, true);
                }
                System.out.println("[BRIDGE] TDLT active with " + tdltMinutes + " minutes remaining.");
                return;
            }

            // Case 2: Check bag for item 521 with > 0 minutes
            int[] bagTdlt = findTdltItemInBag();
            int slot = bagTdlt[0];
            int itemMins = bagTdlt[1];

            if (slot != -1 && itemMins > 0) {
                try {
                    Class<?> clsX = getGameClass("nro.X");
                    if (clsX != null) {
                        Method mUse = getTypedMethod(clsX, "k", true, void.class, int.class);
                        if (mUse != null) {
                            mUse.invoke(null, slot);
                            System.out.println("[BRIDGE] Activated TDLT item 521 from bag slot " + slot + " (" + itemMins + " mins)");
                        }
                    }
                } catch (Throwable ignored) {}
                if (clsCt != null) {
                    Field fCt = getTypedField(clsCt, "cT", boolean.class);
                    if (fCt != null) fCt.setBoolean(null, true);
                }
                return;
            }

            // Case 3: No usable item 521 in bag. Only buy from Santa if ALREADY at Map 5 (never hijack Xmap away from farming map!)
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

            int currentMapId = -1;
            Class<?> clsAv = getGameClass("nro.av");
            if (clsAv != null) {
                try {
                    Field fH = getTypedField(clsAv, "h", int.class);
                    if (fH != null) currentMapId = fH.getInt(null);
                } catch (Throwable ignored) {}
            }

            long now = System.currentTimeMillis();
            if (currentMapId == 5 && totalGems > 0 && clsP != null && (now - lastTdltBuyAttemptMs > 60000L)) {
                lastTdltBuyAttemptMs = now;
                try {
                    Method mAb = getTypedMethod(clsP, "ab", true, void.class);
                    if (mAb != null) {
                        mAb.invoke(null);
                        System.out.println("[BRIDGE] Invoked nro.p.ab() at Map 5 to buy TDLT from Santa!");
                    }
                } catch (Throwable ignored) {}
            }

            // Ensure both in-game cT and Tàn Sát (nro.j.n) are enabled so character attacks mobs immediately on the current map!
            Class<?> clsJ = getGameClass("nro.j");
            if (clsJ != null) {
                Field fN = getTypedField(clsJ, "n", boolean.class);
                if (fN != null) fN.setBoolean(null, true);
            }
            System.out.println("[BRIDGE] TDLT enabled (cT=true, ts=true, slot=" + slot + ", mins=" + itemMins + "); attacking immediately.");
        } catch (Throwable t) {
            System.out.println("[BRIDGE] syncInGameTdltState error: " + t);
        }
    }

    // Idempotently set a mod boolean feature to desired state without accidental toggle-off
    public static boolean setModFlag(String cmd, boolean desired) {
        try {
            ensureModControllerInitialized();
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

            // Set directly on the typed boolean static field
            String[] target = resolveModFlagTarget(c);
            if (target == null) return false;
            Class<?> cls = getGameClass(target[0]);
            if (cls == null) return false;
            Field f = getTypedField(cls, target[1], boolean.class);
            if (f == null) return false;
            if (f.getBoolean(null) != desired) {
                f.setBoolean(null, desired);
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
            // 0. Dismiss any open NPC/confirm dialog that could block movement
            Class<?> clsQ = getGameClass("nro.Q");
            if (clsQ != null) {
                Method mBo = getTypedMethod(clsQ, "bo", true, void.class);
                if (mBo != null) mBo.invoke(null);
            }
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

    // Prevent Xmap from deadlocking if Capsule menu (nro.cr.b.g) fails to open within 1.8 seconds
    public static void checkAndUnstickXmap() {
        try {
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f == null) return;
            Field fE = getTypedField(cls_f, "e", boolean.class);
            Field fH = getTypedField(cls_f, "h", boolean.class);
            Field fG = getTypedField(cls_f, "g", boolean.class);
            Field fB = getTypedField(cls_f, "b", long.class);
            if (fE != null && fE.getBoolean(null) && fH != null && fH.getBoolean(null) && fG != null && !fG.getBoolean(null)) {
                long capStart = (fB != null) ? fB.getLong(null) : 0L;
                if (capStart > 0L && System.currentTimeMillis() - capStart > 1800L) {
                    fG.setBoolean(null, true);
                    System.out.println("[BRIDGE] Unstuck Xmap capsule wait (falling back to portal travel)");
                }
            }
        } catch (Throwable ignored) {}
    }

    // Call Xmap travel to mapId
    public static boolean travelToMap(int mapId) {
        try {
            ensureModControllerInitialized();
            pauseAutoForTravel(mapId);
            // Speed up Xmap step interval (nro.F.J = 350ms instead of 1000ms)
            Class<?> clsF = getGameClass("nro.F");
            if (clsF != null) {
                Field fStepDelay = getTypedField(clsF, "J", long.class);
                if (fStepDelay != null && fStepDelay.getLong(null) > 350L) {
                    fStepDelay.setLong(null, 350L);
                }
            }
            Class<?> cls_f = getGameClass("nro.f");
            if (cls_f != null) {
                // Reset any stale Xmap error/capsule/delay flags before starting new route (keep e=false before b(mapId) so b(mapId) doesn't toggle off!)
                Field fE = getTypedField(cls_f, "e", boolean.class);
                if (fE != null) fE.setBoolean(null, false);
                Field fL = getTypedField(cls_f, "l", int.class);
                if (fL != null) fL.setInt(null, mapId);
                Field fF = getTypedField(cls_f, "f", boolean.class);
                if (fF != null) fF.setBoolean(null, false);
                Field fG = getTypedField(cls_f, "g", boolean.class);
                if (fG != null) fG.setBoolean(null, false);
                Field fH = getTypedField(cls_f, "h", boolean.class);
                if (fH != null) fH.setBoolean(null, false);
                Field fC = getTypedField(cls_f, "c", long.class);
                if (fC != null) fC.setLong(null, 0L);

                // Call nro.f.b(mapId) ONCE to compute route and start Xmap
                Method bMeth = getTypedMethod(cls_f, "b", true, void.class, int.class);
                if (bMeth != null) {
                    bMeth.invoke(null, mapId);
                }

                // Ensure target map and running flag are active if route did not immediately error
                if (fL != null) fL.setInt(null, mapId);
                if (fE != null && (fF == null || !fF.getBoolean(null))) {
                    fE.setBoolean(null, true);
                }

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
            Thread loginWorker = new Thread(
                new GameBridgeAgent(TASK_DIRECT_LOGIN, null, cleanUser, cleanPass, serverNum, false, null, -1, null),
                "NRO-DirectLogin-Worker"
            );
            loginWorker.setDaemon(true);
            loginWorker.start();
            return true;
        } catch (Throwable t) {
            System.out.println("[BRIDGE] directLogin error: " + t);
            return false;
        }
    }

    private static void runDirectLoginWorker(String cleanUser, String cleanPass, int serverNum) {
        try {
            autoLaunchMidletIfNeeded();
            Class<?> clsCr = getGameClass("nro.cr");
            Class<?> clsCJ = getGameClass("nro.cJ");
            Class<?> clsE = getGameClass("nro.e");
            stopXmap();
            // Disconnect active session & switch to LoginScr
            if (clsE != null) {
                Method mH = getTypedMethod(clsE, "h", true, void.class);
                if (mH != null) mH.invoke(null);
            }
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
                    if (cleanUser != null && !cleanUser.isEmpty()) {
                        Field fUser = getTypedFieldByTypeName(clsCJ, "b", "nro.au");
                        Object tfUser = (fUser != null) ? fUser.get(loginScr) : null;
                        if (tfUser != null) {
                            Method mT = getTypedMethod(tfUser.getClass(), "t", false, void.class, String.class);
                            if (mT != null) mT.invoke(tfUser, cleanUser);
                        }
                    }
                    if (cleanPass != null && !cleanPass.isEmpty()) {
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
            Method mJ = (clsE != null) ? getTypedMethod(clsE, "j", true, void.class) : null;
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
                ensureModControllerInitialized();
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
                ensureModControllerInitialized();
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
                ensureModControllerInitialized();
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
                ensureModControllerInitialized();
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

            // 5. Direct Zone Switch ("k<zone>")
            if (lower.startsWith("k") && lower.length() > 1 && Character.isDigit(lower.charAt(1))) {
                try {
                    ensureModControllerInitialized();
                    int targetZone = Integer.parseInt(lower.substring(1).trim());
                    autoFarmZone = targetZone;
                    // Direct network packet via nro.am.a().K(targetZone)
                    Class<?> clsAm = getGameClass("nro.am");
                    if (clsAm != null) {
                        Method mA = getTypedMethodByReturnName(clsAm, "a", true, "nro.am");
                        Object instAm = (mA != null) ? mA.invoke(null) : null;
                        if (instAm != null) {
                            Method mK = getTypedMethod(clsAm, "K", false, void.class, int.class);
                            if (mK != null) mK.invoke(instAm, targetZone);
                        }
                    }
                    // In-game mod zone tracker via nro.q.f(targetZone)
                    Class<?> clsQ = getGameClass("nro.q");
                    if (clsQ != null) {
                        Method mF = getTypedMethod(clsQ, "f", true, void.class, int.class);
                        if (mF != null) mF.invoke(null, targetZone);
                    }
                    updateGobackTarget(-1, targetZone);
                    System.out.println("[BRIDGE] Switched to Zone " + targetZone + " via direct nro.am.K and nro.q.f");
                    return true;
                } catch (Throwable t) {
                    System.out.println("[BRIDGE] Error switching zone: " + t);
                }
            }

            // 5b. Direct Speed Set ("s<speed>", e.g. "s20") via nro.F.I
            if (lower.startsWith("s") && lower.length() > 1 && Character.isDigit(lower.charAt(1))) {
                try {
                    ensureModControllerInitialized();
                    int spd = Integer.parseInt(lower.substring(1).trim());
                    Class<?> clsF = getGameClass("nro.F");
                    if (clsF != null) {
                        Field fSpd = getTypedField(clsF, "I", int.class);
                        if (fSpd != null) fSpd.setInt(null, spd);
                        Method aMeth = getTypedMethodByReturnName(clsF, "a", true, "nro.F");
                        Object instF = (aMeth != null) ? aMeth.invoke(null) : null;
                        if (instF != null) {
                            Method dMeth = getTypedMethod(clsF, "d", false, boolean.class, String.class);
                            if (dMeth != null) dMeth.invoke(instF, lower);
                        }
                    }
                    return true;
                } catch (Throwable ignored) {}
            }

            // 6. Direct TDLT chip toggle
            if ("tdlt".equals(lower)) {
                Boolean curTdlt = getModFlag("tdlt");
                boolean turnOn = (curTdlt == null || !curTdlt.booleanValue());
                if (turnOn) {
                    setModFlag("gb", true);
                    updateGobackTarget(autoFarmMap, autoFarmZone);
                    setModFlag("tdlt", true);
                    syncInGameTdltState(true);
                } else {
                    setModFlag("tdlt", false);
                    syncInGameTdltState(false);
                }
                return true;
            }

            // 7. Direct Tàn Sát chip toggle
            if ("ts".equals(lower)) {
                Boolean curTs = getModFlag("ts");
                boolean turnOn = (curTs == null || !curTs.booleanValue());
                if (turnOn) {
                    configurePunchSkill();
                    setModFlag("ts", true);
                } else {
                    setModFlag("ts", false);
                }
                return true;
            }

            // 8. Direct Goback chip toggle
            if ("gb".equals(lower)) {
                Boolean curGb = getModFlag("gb");
                boolean turnOn = (curGb == null || !curGb.booleanValue());
                setModFlag("gb", turnOn);
                if (turnOn) {
                    updateGobackTarget(-1, -1);
                }
                return true;
            }

            Boolean beforeFlag = getModFlag(lower);

            // 7. Invoke the Hùng Hero command handler (nro.F.a().d(cmd) / e(cmd))
            try {
                Class<?> clsF = getGameClass("nro.F");
                if (clsF != null) {
                    Method aMeth = getTypedMethodByReturnName(clsF, "a", true, "nro.F");
                    Object instF = (aMeth != null) ? aMeth.invoke(null) : null;
                    if (instF != null) {
                        try {
                            Method dMeth = getTypedMethod(clsF, "d", false, boolean.class, String.class);
                            Object resD = (dMeth != null) ? dMeth.invoke(instF, lower) : null;
                            if (Boolean.TRUE.equals(resD)) {
                                handled = true;
                            }
                        } catch (Throwable ignored) {}
                        if (!handled) {
                            try {
                                Method eMeth = getTypedMethod(clsF, "e", false, boolean.class, String.class);
                                Object resE = (eMeth != null) ? eMeth.invoke(instF, lower) : null;
                                if (Boolean.TRUE.equals(resE)) handled = true;
                            } catch (Throwable ignored) {}
                        }
                        System.out.println("[BRIDGE] Executed nro.F cmd [" + lower + "] -> " + handled);
                    }
                }
            } catch (Throwable ignored) {}

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

            // 0. Ensure nro.F singleton is initialized first so nro.F.aE() -> nro.j.I() has already run
            try {
                Class<?> clsF = getGameClass("nro.F");
                if (clsF != null) {
                    Method mFa = getTypedMethodByReturnName(clsF, "a", true, "nro.F");
                    if (mFa != null) mFa.invoke(null);
                }
            } catch (Throwable ignored) {}

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

            // Clear stuck Auto-Pick moving flag (nro.i.m) if Auto-Pick is off so nro.j.H() is never blocked
            try {
                Class<?> clsI = getGameClass("nro.i");
                if (clsI != null) {
                    Field fIl = getTypedField(clsI, "l", boolean.class);
                    Field fIm = getTypedField(clsI, "m", boolean.class);
                    if (fIm != null && (fIl == null || !fIl.getBoolean(null))) {
                        fIm.setBoolean(null, false);
                    }
                }
            } catch (Throwable ignored) {}

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

            // 1. Mark skill ready to attack via nro.j.a(chosenSkill)
            try {
                Method mJa = getTypedMethod(clsJ, "a", true, void.class, chosenSkill.getClass());
                if (mJa != null) {
                    mJa.invoke(null, chosenSkill);
                } else {
                    Field fBu = getTypedField(chosenSkill.getClass(), "bu", boolean.class);
                    if (fBu != null) fBu.setBoolean(chosenSkill, true);
                    Field fAf = getTypedField(chosenSkill.getClass(), "af", long.class);
                    if (fAf != null) fAf.setLong(chosenSkill, System.currentTimeMillis());
                }
            } catch (Throwable ignored) {}

            // 2. Put chosenSkill in nro.j.a[0]
            autoSkills.removeAllElements();
            autoSkills.addElement(chosenSkill);
            for (int i = 1; i < 10; i++) {
                autoSkills.addElement(null);
            }

            // 3. Populate nro.j.b with chosenSkillId so nro.j.E() NEVER wipes nro.j.a to all nulls!
            if (fAutoB != null) {
                Vector pendingB = (Vector) fAutoB.get(null);
                if (pendingB != null) {
                    pendingB.removeAllElements();
                    pendingB.addElement(Integer.valueOf(skillId));
                    for (int i = 1; i < 10; i++) {
                        pendingB.addElement(Integer.valueOf(-1));
                    }
                }
            }

            // 4. Persist into RMS "listSkillsAuto"
            try {
                Class<?> clsAg = getGameClass("nro.ag");
                Class<?> clsY = getGameClass("nro.Y");
                if (clsAg != null && clsY != null) {
                    Method mYb = getTypedMethod(clsY, "b", true, String.class, String.class);
                    if (mYb != null) {
                        String rmsKey = (String) mYb.invoke(null, "\u0004\u001c\u001d\u0013;\u000e\u001b\u0003\u0004\u0006/\u0012\u001c\n");
                        Method mSave = getTypedMethod(clsAg, "b", true, void.class, String.class, String.class);
                        if (mSave != null && rmsKey != null) {
                            mSave.invoke(null, rmsKey, skillId + ",-1,-1,-1,-1,-1,-1,-1,-1,-1,");
                        }
                    }
                }
            } catch (Throwable ignored) {}

            // 5. Ensure hotbarC[0] and hotbarD[0] have chosenSkill
            if (hotbarC != null && hotbarC.length > 0) hotbarC[0] = chosenSkill;
            if (hotbarD != null && hotbarD.length > 0) hotbarD[0] = chosenSkill;

            // 6. Ensure character selected skill (myChar.d) is chosenSkill
            if (myChar != null && clsCd != null) {
                Field fCharSkill = getTypedFieldByTypeName(clsCd, "d", "nro.ao");
                if (fCharSkill != null) {
                    fCharSkill.set(myChar, chosenSkill);
                }
            }

            // 7. Clear any stuck pick / hold flags
            try {
                Class<?> clsI = getGameClass("nro.i");
                if (clsI != null) {
                    Field fIm = getTypedField(clsI, "m", boolean.class);
                    if (fIm != null) fIm.setBoolean(null, false);
                }
                if (myChar != null) {
                    Field fCtChar = getTypedField(myChar.getClass(), "ct", boolean.class);
                    if (fCtChar != null) fCtChar.setBoolean(myChar, false);
                }
            } catch (Throwable ignored) {}

            System.out.println("[BRIDGE] Configured melee skill [" + skillName + "] (id=" + skillId + ") in nro.j.a[0]");

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

    // 24/7 Watchdog Thread: Ensures Auto-Launch from Launcher, Auto-Reconnect (alogin + sz) and Auto-Resume Farm
    private static void startWatchdogThread() {
        Thread wd = new Thread(
            new GameBridgeAgent(TASK_WATCHDOG, null, null, null, 0, false, null, -1, null),
            "NRO-AutoFarm-Watchdog"
        );
        wd.setDaemon(true);
        wd.start();
    }

    private static void runWatchdogLoop() {
        System.out.println("[BRIDGE] 24/7 Auto-Reconnect & AutoFarm Watchdog started.");
        // Fast initial check to auto-start MIDlet from MicroEmulator Launcher as soon as it loads
        for (int bootStep = 0; bootStep < 12 && running; bootStep++) {
            try {
                Thread.sleep(900);
                if (autoLaunchMidletIfNeeded()) {
                    break;
                }
            } catch (Throwable ignored) {}
        }

        while (running) {
            try {
                Thread.sleep(5000);

                // Ensure HTTP bridge server is always alive and listening
                if (serverThread == null || !serverThread.isAlive()) {
                    System.out.println("[WATCHDOG] HTTP Bridge server is down! Auto-restarting HTTP server...");
                    startHttpServer();
                }

                // Always ensure MIDlet is started if MicroEmulator is sitting at the Launcher screen
                if (!isMidletLoaded()) {
                    autoLaunchMidletIfNeeded();
                    continue;
                }

                if (!autoFarmActive) continue;

                // If an explicit farmThread is currently executing travel/setup, let it finish
                if (farmThread != null && farmThread.isAlive()) continue;

                Map<String, Object> st = getStatus();
                boolean inGame = Boolean.TRUE.equals(st.get("in_game"));
                if (!inGame) {
                    continue;
                }

                // Always keep Hùng Hero's built-in Auto-Login (nro.e.c) and Save-Zone (nro.e.d) active once in-game
                setModFlag("alogin", true);
                setModFlag("sz", true);
                setModFlag("checklag", true);

                // Ensure melee skill is in auto list
                configurePunchSkill();

                // If currently flying to Map 5 (Đảo Kame) to auto-buy TDLT from Santa, bound wait to 20s max
                Class<?> clsP = getGameClass("nro.p");
                if (clsP != null) {
                    Field fBuyingTdlt = getTypedField(clsP, "B", boolean.class);
                    if (fBuyingTdlt != null && fBuyingTdlt.getBoolean(null)) {
                        checkAndUnstickXmap();
                        if (buyingTdltSinceMs == 0L) buyingTdltSinceMs = System.currentTimeMillis();
                        if (System.currentTimeMillis() - buyingTdltSinceMs < 20000L) {
                            continue;
                        } else {
                            // Safety timeout after 20s so character never gets stuck trying to reach Map 5
                            fBuyingTdlt.setBoolean(null, false);
                            Field fZ = getTypedField(clsP, "z", boolean.class);
                            if (fZ != null) fZ.setBoolean(null, false);
                            buyingTdltSinceMs = 0L;
                            stopXmap();
                            System.out.println("[WATCHDOG] Auto-buy TDLT at Map 5 timed out after 20s; resuming farm with Tàn Sát.");
                        }
                    } else {
                        buyingTdltSinceMs = 0L;
                    }
                }

                Boolean xmapRun = (Boolean) st.get("xmap_running");
                if (Boolean.TRUE.equals(xmapRun)) {
                    checkAndUnstickXmap();
                    continue;
                }

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

    // Scan an item array (bag or body) for SKH items using type-safe reflection
    private static void scanItemsForSkh(Object[] items, String locationLabel, List<String> skhList) {
        if (items == null) return;
        try {
            Class<?> clsCD = getGameClass("nro.cD");
            Method isSkhMeth = (clsCD != null) ? getTypedMethod(clsCD, "b", true, boolean.class, clsCD) : null;

            for (int i = 0; i < items.length; i++) {
                Object item = items[i];
                if (item == null) continue;
                Field fTpl = getTypedFieldByTypeName(item.getClass(), "a", "nro.cH");
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
        status.put("midlet_loaded", isMidletLoaded());
        status.put("autofarm_active", autoFarmActive);
        status.put("autofarm_map", autoFarmMap);
        status.put("autofarm_zone", autoFarmZone);
        status.put("autofarm_mode", autoFarmMode);

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

                // Scan bag and body for SKH items and slot usage (only when inGame is true)
                if (inGame) {
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

                    // Read character learned skills
                    List<Map<String, Object>> skillList = new ArrayList<Map<String, Object>>();
                    try {
                        Field fL = getTypedFieldByTypeName(cCls, "L", "nro.cS");
                        if (fL != null) {
                            Vector learned = (Vector) fL.get(charObj);
                            if (learned != null) {
                                for (int i = 0; i < learned.size(); i++) {
                                    Object s = learned.elementAt(i);
                                    if (s != null) {
                                        Map<String, Object> skMap = new LinkedHashMap<String, Object>();
                                        int sId = getSkillTemplateId(s);
                                        String sName = getSkillTemplateName(s);
                                        Field fPoint = getTypedField(s.getClass(), "bU", int.class);
                                        int pt = (fPoint != null) ? fPoint.getInt(s) : 0;
                                        skMap.put("index", i + 1);
                                        skMap.put("id", sId);
                                        skMap.put("name", sName);
                                        skMap.put("point", pt);
                                        skMap.put("is_melee", isBasicMeleeSkill(s));
                                        skillList.add(skMap);
                                    }
                                }
                            }
                        }
                    } catch (Throwable t) {
                        System.out.println("[BRIDGE] Error reading character skills: " + t);
                    }
                    status.put("skills", skillList);

                    // Read hotbar skills (slots 1 to 5)
                    List<Map<String, Object>> hotbarList = new ArrayList<Map<String, Object>>();
                    try {
                        Class<?> clsCt = getGameClass("nro.ct");
                        Class<?> clsCr = getGameClass("nro.cr");
                        if (clsCt != null && clsCr != null) {
                            Field fCs = getTypedField(clsCr, "cS", boolean.class);
                            boolean alternate = (fCs != null) && fCs.getBoolean(null);
                            Field fHotbar = getTypedFieldByTypeName(clsCt, alternate ? "c" : "d", "nro.ao[]");
                            if (fHotbar != null) {
                                Object[] hb = (Object[]) fHotbar.get(null);
                                if (hb != null) {
                                    for (int i = 0; i < hb.length; i++) {
                                        Object s = hb[i];
                                        Map<String, Object> hbMap = new LinkedHashMap<String, Object>();
                                        hbMap.put("slot", i + 1);
                                        if (s != null) {
                                            hbMap.put("id", getSkillTemplateId(s));
                                            hbMap.put("name", getSkillTemplateName(s));
                                        } else {
                                            hbMap.put("id", -1);
                                            hbMap.put("name", "Trống");
                                        }
                                        hotbarList.add(hbMap);
                                    }
                                }
                            }
                        }
                    } catch (Throwable ignored) {}
                    status.put("hotbar", hotbarList);

                    // Read auto attack skill list (nro.j.a)
                    List<String> autoSkillNames = new ArrayList<String>();
                    try {
                        Class<?> clsJ = getGameClass("nro.j");
                        if (clsJ != null) {
                            Field fAutoA = getTypedField(clsJ, "a", Vector.class);
                            if (fAutoA != null) {
                                Vector av = (Vector) fAutoA.get(null);
                                if (av != null) {
                                    for (int i = 0; i < av.size(); i++) {
                                        Object s = av.elementAt(i);
                                        if (s != null) {
                                            autoSkillNames.add(getSkillTemplateName(s));
                                        }
                                    }
                                }
                            }
                        }
                    } catch (Throwable ignored) {}
                    status.put("auto_skills", autoSkillNames);
                }
            }
        } catch (Throwable t) {
            System.out.println("[BRIDGE] getStatus char telemetry error: " + t);
        }

        status.put("in_game", inGame);

        // Only read mod classes (nro.F, nro.f, nro.p, nro.j, etc.) when inGame is true so we never trigger <clinit> before GameScr is ready!
        String[] trackedFlags = new String[]{"ts", "tdlt", "gb", "anhat", "locdo", "xdh", "annguoi", "alogin", "sz", "kvt", "nb", "cflag"};
        if (inGame) {
            try {
                Class<?> cls_f = getGameClass("nro.f");
                if (cls_f != null) {
                    Field fE = getTypedField(cls_f, "e", boolean.class);
                    Field fL = getTypedField(cls_f, "l", int.class);
                    if (fE != null) status.put("xmap_running", fE.getBoolean(null));
                    if (fL != null) status.put("target_map", fL.getInt(null));
                }
            } catch (Throwable ignored) {}

            for (int i = 0; i < trackedFlags.length; i++) {
                String fName = trackedFlags[i];
                Boolean val = getModFlag(fName);
                status.put("mod_" + fName, Boolean.TRUE.equals(val));
            }

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
        } else {
            status.put("xmap_running", false);
            status.put("target_map", -1);
            for (int i = 0; i < trackedFlags.length; i++) {
                status.put("mod_" + trackedFlags[i], false);
            }
            status.put("ingame_tdlt", false);
            status.put("tdlt_minutes", -1);
        }

        return status;
    }

    private static synchronized void startHttpServer() {
        if (serverThread != null && serverThread.isAlive()) return;
        serverThread = new Thread(
            new GameBridgeAgent(TASK_HTTP_SERVER, null, null, null, 0, false, null, -1, null),
            "NRO-Bridge-Server"
        );
        serverThread.setDaemon(true);
        serverThread.start();
    }

    private static void runHttpServerLoop() {
        int retryCount = 0;
        while (running) {
            ServerSocket serverSocket = null;
            try {
                serverSocket = new ServerSocket();
                serverSocket.setReuseAddress(true);
                serverSocket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), serverPort), 50);
                System.out.println("[BRIDGE] HTTP Bridge server listening on 127.0.0.1:" + serverPort);
                retryCount = 0;

                while (running) {
                    try {
                        final Socket socket = serverSocket.accept();
                        socket.setSoTimeout(10000);
                        socket.setTcpNoDelay(true);
                        getHttpExecutor().execute(
                            new GameBridgeAgent(TASK_HANDLE_CLIENT, socket, null, null, 0, false, null, -1, null)
                        );
                    } catch (Throwable clientErr) {
                        if (!running) break;
                        try { Thread.sleep(50); } catch (Throwable ignored) {}
                    }
                }
            } catch (Throwable t) {
                System.out.println("[BRIDGE] Server socket error on port " + serverPort + ": " + t);
                retryCount++;
            } finally {
                if (serverSocket != null && !serverSocket.isClosed()) {
                    try { serverSocket.close(); } catch (Throwable ignored) {}
                }
            }
            if (running) {
                try {
                    long waitMs = Math.min(1000L * retryCount, 4000L);
                    if (waitMs < 500L) waitMs = 500L;
                    Thread.sleep(waitMs);
                } catch (Throwable ignored) {}
            }
        }
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
            } else if (path.equals("/api/game/auto_launch")) {
                boolean ok = autoLaunchMidletIfNeeded();
                jsonResp = "{\"status\": \"ok\", \"midlet_loaded\": " + ok + "}";
            } else if (path.equals("/api/game/input_key")) {
                String k = params.get("key");
                boolean ok = sendKey(k);
                if (ok) {
                    jsonResp = "{\"status\": \"ok\", \"key\": \"" + escapeJson(k) + "\"}";
                } else {
                    statusCode = 400;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Could not dispatch key\"}";
                }
            } else if (path.equals("/api/game/input_click")) {
                int x = 120;
                int y = 180;
                try { x = Math.round(Float.parseFloat(params.get("x"))); } catch (Throwable ignored) {}
                try { y = Math.round(Float.parseFloat(params.get("y"))); } catch (Throwable ignored) {}
                boolean ok = sendPointerClick(x, y);
                if (ok) {
                    jsonResp = "{\"status\": \"ok\", \"x\": " + x + ", \"y\": " + y + "}";
                } else {
                    statusCode = 400;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Could not dispatch click\"}";
                }
            } else if (path.equals("/api/game/input_text")) {
                String txt = params.get("text") != null ? params.get("text") : "";
                String fieldHint = params.get("field");
                String submit = params.get("submit");
                boolean ok = sendTextInput(txt, fieldHint, submit);
                if (ok) {
                    jsonResp = "{\"status\": \"ok\"}";
                } else {
                    statusCode = 400;
                    jsonResp = "{\"status\": \"error\", \"error\": \"No active text field found\"}";
                }
            } else if (path.equals("/api/game/clear_text")) {
                boolean ok = clearActiveText();
                jsonResp = "{\"status\": \"ok\", \"cleared\": " + ok + "}";
            } else if (path.equals("/api/game/open_account_dialog")) {
                boolean ok = openAccountDialog();
                if (ok) {
                    jsonResp = "{\"status\": \"ok\"}";
                } else {
                    statusCode = 500;
                    jsonResp = "{\"status\": \"error\", \"error\": \"Could not open LoginScr\"}";
                }
            } else if (path.equals("/api/game/xmap")) {
                String mapStr = params.get("map");
                if (mapStr != null) {
                    int mapId = Integer.parseInt(mapStr);
                    if (autoFarmActive) {
                        autoFarmMap = mapId;
                        saveAutoFarmState();
                    }
                    lastWatchdogTravelMs = System.currentTimeMillis();
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
                autoLaunchMidletIfNeeded();
                stopXmap();
                dismissDialog();
                try {
                    Class<?> clsQ = getGameClass("nro.Q");
                    if (clsQ != null) {
                        Method mBo = getTypedMethod(clsQ, "bo", true, void.class);
                        if (mBo != null) mBo.invoke(null);
                    }
                    Class<?> clsBR = getGameClass("nro.bR");
                    if (clsBR != null) {
                        Field fStaticChat = getTypedFieldByTypeName(clsBR, "a", "nro.bR");
                        Object chatInst = (fStaticChat != null) ? fStaticChat.get(null) : null;
                        if (chatInst != null) {
                            Field fShow = getTypedField(clsBR, "bf", boolean.class);
                            if (fShow != null) fShow.setBoolean(chatInst, false);
                        }
                    }
                    Class<?> clsI = getGameClass("nro.i");
                    if (clsI != null) {
                        Field fIm = getTypedField(clsI, "m", boolean.class);
                        if (fIm != null) fIm.setBoolean(null, false);
                    }
                } catch (Throwable ignored) {}
                repaintDisplay();
                jsonResp = "{\"status\": \"ok\"}";
            } else if (path.equals("/api/game/dismiss_dialog")) {
                dismissDialog();
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
                
                // If user didn't pick a map and character is sitting in a safe town (no monsters),
                // auto-select the first mob training map for this planet:
                int targetMapCandidate = parsedMap;
                if (targetMapCandidate < 0 && initialCurMap != null) {
                    int curM = initialCurMap.intValue();
                    if (curM == 0) targetMapCandidate = 1;        // Trái Đất: Làng Aru -> Đồi hoa cúc
                    else if (curM == 7) targetMapCandidate = 8;   // Namếc: Làng Mori -> Đồi nấm tím
                    else if (curM == 14) targetMapCandidate = 15; // Xayda: Làng Kakarot -> Đồi hoang
                }
                final int effectiveTargetMap = targetMapCandidate;
                if (effectiveTargetMap >= 0) {
                    autoFarmMap = effectiveTargetMap;
                    saveAutoFarmState();
                }

                final boolean needMapTravel = (effectiveTargetMap >= 0 && (initialCurMap == null || initialCurMap.intValue() != effectiveTargetMap));

                setModFlag("alogin", true);
                setModFlag("sz", true);
                setModFlag("checklag", true);

                if (needMapTravel) {
                    // Pause conflicting auto loops and start Xmap immediately on this request
                    lastWatchdogTravelMs = System.currentTimeMillis();
                    travelToMap(effectiveTargetMap);
                } else {
                    // Already at target map (or keeping current map): apply flags immediately
                    applyFarmModeFlags(mode);
                    setModFlag("locdo", true);
                    setModFlag("anhat", true);
                    executeModAction("s20");
                }

                farmThread = new Thread(
                    new GameBridgeAgent(TASK_AUTO_FARM, null, null, null, 0, needMapTravel, initialCurMap, effectiveTargetMap, mode),
                    "AutoFarm-Worker"
                );
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
        } catch (Throwable ignored) {
        } finally {
            try { socket.close(); } catch (Throwable ignored) {}
        }
    }

    private static void runAutoFarmWorker(boolean needMapTravel, Integer initialCurMap, int effectiveTargetMap, String mode) {
        try {
            // 1. If target map specified and not there yet, monitor Xmap travel until arrival
            if (needMapTravel) {
                System.out.println("[BRIDGE] AutoFarm: Traveling from map " + initialCurMap + " to map " + effectiveTargetMap + "...");
                long startWait = System.currentTimeMillis();
                boolean arrived = false;
                int retryCount = 0;
                Integer lastProgressMap = initialCurMap;

                while (System.currentTimeMillis() - startWait < 60000L) {
                    Thread.sleep(600);
                    checkAndUnstickXmap();
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

                    if (cm != null && cm.intValue() == effectiveTargetMap) {
                        if (Boolean.TRUE.equals(xmapRun)) {
                            stopXmap();
                        }
                        System.out.println("[BRIDGE] AutoFarm: Arrived at target map " + effectiveTargetMap + "!");
                        arrived = true;
                        break;
                    }

                    if (cm != null && (lastProgressMap == null || cm.intValue() != lastProgressMap.intValue())) {
                        lastProgressMap = cm;
                        retryCount = 0;
                    }

                    // If Xmap stopped before reaching effectiveTargetMap, retry up to 2 times
                    if (!Boolean.TRUE.equals(xmapRun)) {
                        if (retryCount < 2) {
                            retryCount++;
                            System.out.println("[BRIDGE] AutoFarm: Xmap paused at map " + cm + ", retrying travel to " + effectiveTargetMap + " (" + retryCount + "/2)...");
                            lastWatchdogTravelMs = System.currentTimeMillis();
                            travelToMap(effectiveTargetMap);
                            Thread.sleep(1000);
                        } else {
                            System.out.println("[BRIDGE] AutoFarm: Map " + effectiveTargetMap + " unreachable from map " + cm + "; farming at current map " + cm);
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

            // 2. Switch zone only if explicitly specified AND character is not already in that zone
            if (autoFarmZone >= 0) {
                Map<String, Object> stAfter = getStatus();
                Integer cz = (Integer) stAfter.get("zone");
                if (cz == null || cz.intValue() != autoFarmZone) {
                    executeModAction("k" + autoFarmZone);
                    Thread.sleep(600);
                }
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
