package com.synapsetw.app;

import android.content.res.AssetManager;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URL;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Lightweight local HTTP server serving bundled SYNAPSE TW web assets on 127.0.0.1
 * and providing an allowlisted CORS-free proxy for market quotes & AI provider APIs.
 */
public final class LocalAssetServer {
    private static final Set<String> ALLOWED_PROXY_HOSTS = new HashSet<>(Arrays.asList(
            "api.groq.com",
            "generativelanguage.googleapis.com",
            "api.openai.com",
            "api.x.ai",
            "api.anthropic.com",
            "api.finmindtrade.com",
            "query1.finance.yahoo.com",
            "query2.finance.yahoo.com"
    ));

    private final AssetManager assets;
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private ServerSocket serverSocket;
    private volatile boolean running = false;
    private int boundPort = 18432;

    public LocalAssetServer(AssetManager assets) {
        this.assets = assets;
    }

    public int start(int preferredPort) throws IOException {
        try {
            serverSocket = new ServerSocket(preferredPort, 32, InetAddress.getByName("127.0.0.1"));
        } catch (IOException e) {
            serverSocket = new ServerSocket(0, 32, InetAddress.getByName("127.0.0.1"));
        }
        boundPort = serverSocket.getLocalPort();
        running = true;
        pool.execute(() -> {
            while (running && serverSocket != null && !serverSocket.isClosed()) {
                try {
                    Socket client = serverSocket.accept();
                    pool.execute(() -> handleClient(client));
                } catch (IOException ignored) {
                    if (!running) break;
                }
            }
        });
        return boundPort;
    }

    public int getPort() {
        return boundPort;
    }

    public void stop() {
        running = false;
        if (serverSocket != null) {
            try {
                serverSocket.close();
            } catch (IOException ignored) {
            }
        }
        pool.shutdownNow();
    }

    private void handleClient(Socket socket) {
        try (Socket s = socket) {
            s.setSoTimeout(30000);
            BufferedInputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = s.getOutputStream();

            String requestLine = readLine(in);
            if (requestLine == null || requestLine.isEmpty()) return;
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) return;

            String method = parts[0].trim().toUpperCase(Locale.US);
            String fullPath = parts[1].trim();

            Map<String, String> headers = new HashMap<>();
            String line;
            while ((line = readLine(in)) != null && !line.isEmpty()) {
                int idx = line.indexOf(':');
                if (idx > 0) {
                    headers.put(line.substring(0, idx).trim().toLowerCase(Locale.US), line.substring(idx + 1).trim());
                }
            }

            byte[] reqBody = new byte[0];
            String clStr = headers.get("content-length");
            if (clStr != null) {
                int cl = Math.max(0, Math.min(Integer.parseInt(clStr.trim()), 4 * 1024 * 1024));
                if (cl > 0) {
                    reqBody = readExact(in, cl);
                }
            }

            if ("OPTIONS".equals(method)) {
                writeResponse(out, 204, "No Content", "text/plain", new byte[0]);
                return;
            }

            if (fullPath.startsWith("/_android_proxy")) {
                handleProxy(out, method, fullPath, headers, reqBody);
                return;
            }

            String cleanPath = fullPath.split("\\?", 2)[0];
            if ("/".equals(cleanPath) || cleanPath.isEmpty()) {
                cleanPath = "/index.html";
            }
            if (cleanPath.contains("..")) {
                writeResponse(out, 403, "Forbidden", "text/plain; charset=utf-8", "Forbidden".getBytes(StandardCharsets.UTF_8));
                return;
            }
            String assetRel = cleanPath.startsWith("/") ? cleanPath.substring(1) : cleanPath;
            try (InputStream assetIn = assets.open(assetRel)) {
                byte[] bytes = readAll(assetIn);
                writeResponse(out, 200, "OK", guessContentType(assetRel), bytes);
            } catch (IOException notFound) {
                writeResponse(out, 404, "Not Found", "text/plain; charset=utf-8", "Not Found".getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    private void handleProxy(OutputStream out, String method, String fullPath, Map<String, String> reqHeaders, byte[] reqBody) throws IOException {
        String targetUrl = extractQueryParam(fullPath, "url");
        if (targetUrl == null || targetUrl.isEmpty()) {
            writeResponse(out, 400, "Bad Request", "application/json", "{\"error\":\"missing url\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }
        URL url;
        try {
            url = new URL(targetUrl);
        } catch (Exception e) {
            writeResponse(out, 400, "Bad Request", "application/json", "{\"error\":\"invalid url\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }
        String host = url.getHost() == null ? "" : url.getHost().toLowerCase(Locale.US);
        if (!"https".equalsIgnoreCase(url.getProtocol()) || !ALLOWED_PROXY_HOSTS.contains(host)) {
            writeResponse(out, 403, "Forbidden", "application/json", "{\"error\":\"host not allowed\"}".getBytes(StandardCharsets.UTF_8));
            return;
        }

        HttpURLConnection conn = (HttpURLConnection) url.openConnection();
        conn.setConnectTimeout(10000);
        conn.setReadTimeout(26000);
        conn.setRequestMethod(method);
        conn.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android 14) SynapseTW/3.4");
        conn.setRequestProperty("Accept", "application/json");

        String[] forwardKeys = {"authorization", "content-type", "x-api-key", "anthropic-version"};
        for (String k : forwardKeys) {
            String v = reqHeaders.get(k);
            if (v != null && !v.isEmpty()) {
                conn.setRequestProperty(k, v);
            }
        }

        if (("POST".equals(method) || "PUT".equals(method)) && reqBody.length > 0) {
            conn.setDoOutput(true);
            try (OutputStream cos = conn.getOutputStream()) {
                cos.write(reqBody);
            }
        }

        int code;
        byte[] respBytes;
        String respType = "application/json; charset=utf-8";
        try {
            code = conn.getResponseCode();
            if (conn.getContentType() != null) respType = conn.getContentType();
            InputStream stream = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            respBytes = stream != null ? readAll(stream) : new byte[0];
        } catch (IOException io) {
            code = 502;
            respBytes = ("{\"error\":\"upstream unreachable\"}").getBytes(StandardCharsets.UTF_8);
        } finally {
            conn.disconnect();
        }
        writeResponse(out, code, code == 200 ? "OK" : "Upstream", respType, respBytes);
    }

    private static void writeResponse(OutputStream out, int code, String reason, String contentType, byte[] body) throws IOException {
        String header = "HTTP/1.1 " + code + " " + reason + "\r\n"
                + "Content-Type: " + contentType + "\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Headers: *\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n\r\n";
        out.write(header.getBytes(StandardCharsets.US_ASCII));
        if (body.length > 0) {
            out.write(body);
        }
        out.flush();
    }

    private static String extractQueryParam(String fullPath, String key) {
        int q = fullPath.indexOf('?');
        if (q < 0) return null;
        String qs = fullPath.substring(q + 1);
        for (String pair : qs.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && key.equals(pair.substring(0, eq))) {
                try {
                    return URLDecoder.decode(pair.substring(eq + 1), "UTF-8");
                } catch (Exception e) {
                    return null;
                }
            }
        }
        return null;
    }

    private static String guessContentType(String path) {
        String p = path.toLowerCase(Locale.US);
        if (p.endsWith(".html")) return "text/html; charset=utf-8";
        if (p.endsWith(".js")) return "application/javascript; charset=utf-8";
        if (p.endsWith(".webmanifest") || p.endsWith(".json")) return "application/manifest+json; charset=utf-8";
        if (p.endsWith(".png")) return "image/png";
        if (p.endsWith(".css")) return "text/css; charset=utf-8";
        if (p.endsWith(".svg")) return "image/svg+xml";
        return "application/octet-stream";
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int b;
        while ((b = in.read()) != -1) {
            if (b == '\n') break;
            if (b != '\r') buf.write(b);
            if (buf.size() > 16384) break;
        }
        if (b == -1 && buf.size() == 0) return null;
        return new String(buf.toByteArray(), StandardCharsets.ISO_8859_1);
    }

    private static byte[] readExact(InputStream in, int len) throws IOException {
        byte[] buf = new byte[len];
        int off = 0;
        while (off < len) {
            int r = in.read(buf, off, len - off);
            if (r < 0) break;
            off += r;
        }
        return off == len ? buf : Arrays.copyOf(buf, off);
    }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] chunk = new byte[8192];
        int r;
        while ((r = in.read(chunk)) != -1) {
            out.write(chunk, 0, r);
        }
        return out.toByteArray();
    }
}
