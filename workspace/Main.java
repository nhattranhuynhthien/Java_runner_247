import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Scanner;

public class Main {
    private static volatile boolean running = true;
    private static long startTime = System.currentTimeMillis();
    private static long loopCount = 0;

    public static void main(String[] args) {
        System.out.println("=================================================");
        System.out.println("  Java 24/7 Worker Process Started Successfully  ");
        System.out.println("=================================================");
        System.out.println("Java Version  : " + System.getProperty("java.version"));
        System.out.println("Java Vendor   : " + System.getProperty("java.vendor"));
        System.out.println("OS Name       : " + System.getProperty("os.name"));
        System.out.println("Processors    : " + Runtime.getRuntime().availableProcessors());
        System.out.println("Max Memory    : " + (Runtime.getRuntime().maxMemory() / (1024 * 1024)) + " MB");
        System.out.println("Total Memory  : " + (Runtime.getRuntime().totalMemory() / (1024 * 1024)) + " MB");
        System.out.println("=================================================");
        System.out.println("Type 'help' in console input for interactive commands.\n");

        // Start background thread to handle System.in (Console Input)
        Thread inputThread = new Thread(() -> {
            try (Scanner scanner = new Scanner(System.in)) {
                while (running && scanner.hasNextLine()) {
                    String input = scanner.nextLine().trim();
                    handleCommand(input);
                }
            } catch (Exception e) {
                // System.in stream closed
            }
        });
        inputThread.setDaemon(true);
        inputThread.start();

        // Main continuous 24/7 heartbeat loop
        DateTimeFormatter dtf = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");
        while (running) {
            loopCount++;
            long uptimeSeconds = (System.currentTimeMillis() - startTime) / 1000;
            long usedMem = (Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()) / (1024 * 1024);

            System.out.printf("[%s] [HEARTBEAT #%d] Uptime: %ds | Memory: %dMB | System Status: OK%n",
                    dtf.format(LocalDateTime.now()),
                    loopCount,
                    uptimeSeconds,
                    usedMem
            );

            try {
                // Heartbeat interval: 10 seconds
                Thread.sleep(10000);
            } catch (InterruptedException e) {
                System.out.println("[INFO] Heartbeat interrupted, shutting down...");
                break;
            }
        }

        System.out.println("[INFO] Java Worker Process stopped gracefully.");
    }

    private static void handleCommand(String cmd) {
        if (cmd.isEmpty()) return;
        System.out.println(">> [STDIN RECEIVED]: " + cmd);

        switch (cmd.toLowerCase()) {
            case "help":
                System.out.println("--- Available Commands ---");
                System.out.println("  ping    - Test responsiveness");
                System.out.println("  status  - Print current worker stats");
                System.out.println("  time    - Show server time");
                System.out.println("  mem     - Show detailed memory info");
                System.out.println("  gc      - Request garbage collection");
                System.out.println("  exit    - Gracefully exit the program");
                System.out.println("--------------------------");
                break;
            case "ping":
                System.out.println("<< PONG! Worker is responsive and healthy.");
                break;
            case "status":
                long uptimeSec = (System.currentTimeMillis() - startTime) / 1000;
                System.out.printf("<< Status: RUNNING | Uptime: %d seconds | Heartbeats: %d%n", uptimeSec, loopCount);
                break;
            case "time":
                System.out.println("<< Current Server Time: " + LocalDateTime.now());
                break;
            case "mem":
                long free = Runtime.getRuntime().freeMemory() / (1024 * 1024);
                long total = Runtime.getRuntime().totalMemory() / (1024 * 1024);
                long max = Runtime.getRuntime().maxMemory() / (1024 * 1024);
                System.out.printf("<< Memory Usage: %dMB Used / %dMB Allocated (Max: %dMB)%n", (total - free), total, max);
                break;
            case "gc":
                System.out.println("<< Requesting System.gc()...");
                System.gc();
                System.out.println("<< GC completed.");
                break;
            case "exit":
                System.out.println("<< Exiting process as requested...");
                running = false;
                System.exit(0);
                break;
            default:
                System.out.println("<< Unknown command: '" + cmd + "'. Type 'help' for available commands.");
                break;
        }
    }
}

