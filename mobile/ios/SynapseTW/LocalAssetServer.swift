import Foundation
import Darwin

/// Lightweight pure-Swift local HTTP server on 127.0.0.1 serving bundled SYNAPSE TW
/// web assets and providing an allowlisted CORS-free proxy for market quotes & AI APIs.
final class LocalAssetServer {
    private static let allowedProxyHosts: Set<String> = [
        "api.groq.com",
        "generativelanguage.googleapis.com",
        "api.openai.com",
        "api.x.ai",
        "api.anthropic.com",
        "api.finmindtrade.com",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "mis.twse.com.tw",
        "mis.taifex.com.tw",
        "api.telegram.org",
        "api.fugle.tw",
        "api.github.com"
    ]

    private let assetsRoot: URL
    private let queue = DispatchQueue(label: "com.synapsetw.localserver", attributes: .concurrent)
    private var listenFd: Int32 = -1
    private var running = false
    private(set) var port: UInt16 = 18432

    private lazy var urlSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 26
        cfg.timeoutIntervalForResource = 30
        return URLSession(configuration: cfg)
    }()

    init(assetsRoot: URL) {
        self.assetsRoot = assetsRoot
    }

    func start(preferredPort: UInt16 = 18432) -> UInt16? {
        if let bound = bindSocket(port: preferredPort) {
            return bound
        }
        return bindSocket(port: 0)
    }

    func stop() {
        running = false
        if listenFd >= 0 {
            Darwin.close(listenFd)
            listenFd = -1
        }
    }

    private func bindSocket(port targetPort: UInt16) -> UInt16? {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }

        var reuse: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = targetPort.bigEndian
        addr.sin_addr = in_addr(s_addr: UInt32(INADDR_LOOPBACK).bigEndian)

        let bindRes = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindRes == 0, Darwin.listen(fd, 32) == 0 else {
            Darwin.close(fd)
            return nil
        }

        var boundAddr = sockaddr_in()
        var boundLen = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &boundAddr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.getsockname(fd, $0, &boundLen)
            }
        }

        self.port = UInt16(bigEndian: boundAddr.sin_port)
        self.listenFd = fd
        self.running = true

        queue.async { [weak self] in
            self?.acceptLoop()
        }
        return self.port
    }

    private func acceptLoop() {
        while running && listenFd >= 0 {
            var clientAddr = sockaddr_in()
            var clientLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let clientFd = withUnsafeMutablePointer(to: &clientAddr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.accept(listenFd, $0, &clientLen)
                }
            }
            if clientFd < 0 {
                if !running { break }
                continue
            }
            var nosigpipe: Int32 = 1
            setsockopt(clientFd, SOL_SOCKET, SO_NOSIGPIPE, &nosigpipe, socklen_t(MemoryLayout<Int32>.size))
            queue.async { [weak self] in
                self?.handleClient(fd: clientFd)
            }
        }
    }

    private func handleClient(fd: Int32) {
        defer { Darwin.close(fd) }

        guard let requestData = readHttpRequest(fd: fd),
              let headerEndRange = requestData.range(of: Data("\r\n\r\n".utf8)) else {
            return
        }

        let headerBytes = requestData.subdata(in: 0..<headerEndRange.lowerBound)
        let bodyBytes = requestData.subdata(in: headerEndRange.upperBound..<requestData.count)

        guard let headerText = String(data: headerBytes, encoding: .utf8) ?? String(data: headerBytes, encoding: .isoLatin1) else {
            return
        }
        let lines = headerText.components(separatedBy: "\r\n")
        guard let firstLine = lines.first else { return }
        let parts = firstLine.split(separator: " ")
        guard parts.count >= 2 else { return }

        let method = String(parts[0]).uppercased()
        let fullPath = String(parts[1])

        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            if let idx = line.firstIndex(of: ":") {
                let k = line[..<idx].trimmingCharacters(in: .whitespaces).lowercased()
                let v = line[line.index(after: idx)...].trimmingCharacters(in: .whitespaces)
                headers[k] = v
            }
        }

        if method == "OPTIONS" {
            sendResponse(fd: fd, code: 204, reason: "No Content", contentType: "text/plain", body: Data())
            return
        }

        if fullPath.hasPrefix("/_mobile_proxy") || fullPath.hasPrefix("/_android_proxy") {
            handleProxy(fd: fd, method: method, fullPath: fullPath, reqHeaders: headers, reqBody: bodyBytes)
            return
        }

        var cleanPath = fullPath.split(separator: "?", maxSplits: 1).first.map(String.init) ?? "/index.html"
        if cleanPath == "/" || cleanPath.isEmpty {
            cleanPath = "/index.html"
        }
        if cleanPath.contains("..") {
            sendResponse(fd: fd, code: 403, reason: "Forbidden", contentType: "text/plain; charset=utf-8", body: Data("Forbidden".utf8))
            return
        }
        let rel = cleanPath.hasPrefix("/") ? String(cleanPath.dropFirst()) : cleanPath
        let fileUrl = assetsRoot.appendingPathComponent(rel)
        if let fileData = try? Data(contentsOf: fileUrl) {
            sendResponse(fd: fd, code: 200, reason: "OK", contentType: guessContentType(rel), body: fileData)
        } else {
            sendResponse(fd: fd, code: 404, reason: "Not Found", contentType: "text/plain; charset=utf-8", body: Data("Not Found".utf8))
        }
    }

    private func handleProxy(
        fd: Int32,
        method: String,
        fullPath: String,
        reqHeaders: [String: String],
        reqBody: Data
    ) {
        guard let comps = URLComponents(string: "http://127.0.0.1" + fullPath),
              let rawTarget = comps.queryItems?.first(where: { $0.name == "url" })?.value,
              let targetUrl = URL(string: rawTarget),
              let scheme = targetUrl.scheme?.lowercased(),
              let host = targetUrl.host?.lowercased() else {
            sendResponse(fd: fd, code: 400, reason: "Bad Request", contentType: "application/json", body: Data("{\"error\":\"invalid url\"}".utf8))
            return
        }

        guard scheme == "https", Self.allowedProxyHosts.contains(host) else {
            sendResponse(fd: fd, code: 403, reason: "Forbidden", contentType: "application/json", body: Data("{\"error\":\"host not allowed\"}".utf8))
            return
        }

        var request = URLRequest(url: targetUrl)
        request.httpMethod = method
        request.setValue("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) SynapseTW/3.5", forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        for k in ["authorization", "content-type", "x-api-key", "anthropic-version", "x-goog-api-key"] {
            if let v = reqHeaders[k], !v.isEmpty {
                request.setValue(v, forHTTPHeaderField: k)
            }
        }
        if (method == "POST" || method == "PUT") && !reqBody.isEmpty {
            request.httpBody = reqBody
        }

        let sem = DispatchSemaphore(value: 0)
        var statusCode = 502
        var respType = "application/json; charset=utf-8"
        var respData = Data("{\"error\":\"upstream unreachable\"}".utf8)

        let task = urlSession.dataTask(with: request) { data, response, _ in
            if let http = response as? HTTPURLResponse {
                statusCode = http.statusCode
                if let ct = http.value(forHTTPHeaderField: "Content-Type"), !ct.isEmpty {
                    respType = ct
                }
            }
            if let d = data {
                respData = d
            }
            sem.signal()
        }
        task.resume()
        _ = sem.wait(timeout: .now() + 28)

        sendResponse(fd: fd, code: statusCode, reason: statusCode == 200 ? "OK" : "Upstream", contentType: respType, body: respData)
    }

    private func readHttpRequest(fd: Int32) -> Data? {
        var tv = timeval(tv_sec: 10, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 8192)
        var expectedBodyLength: Int?
        var headerEndOffset: Int?

        while true {
            let n = Darwin.recv(fd, &chunk, chunk.count, 0)
            if n <= 0 { break }
            buffer.append(chunk, count: n)

            if headerEndOffset == nil, let range = buffer.range(of: Data("\r\n\r\n".utf8)) {
                headerEndOffset = range.upperBound
                if let hdrStr = String(data: buffer.subdata(in: 0..<range.lowerBound), encoding: .isoLatin1) {
                    for line in hdrStr.components(separatedBy: "\r\n") {
                        if line.lowercased().hasPrefix("content-length:") {
                            let val = line.split(separator: ":", maxSplits: 1).last?.trimmingCharacters(in: .whitespaces) ?? "0"
                            expectedBodyLength = min(max(Int(val) ?? 0, 0), 4 * 1024 * 1024)
                        }
                    }
                }
                if (expectedBodyLength ?? 0) == 0 { break }
            }
            if let hEnd = headerEndOffset, let bodyLen = expectedBodyLength {
                if buffer.count >= hEnd + bodyLen { break }
            }
            if buffer.count > 5 * 1024 * 1024 { break }
        }
        return buffer.isEmpty ? nil : buffer
    }

    private func sendResponse(fd: Int32, code: Int, reason: String, contentType: String, body: Data) {
        let header = "HTTP/1.1 \(code) \(reason)\r\n"
            + "Content-Type: \(contentType)\r\n"
            + "Content-Length: \(body.count)\r\n"
            + "Access-Control-Allow-Origin: *\r\n"
            + "Access-Control-Allow-Headers: *\r\n"
            + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            + "Cache-Control: no-store\r\n"
            + "Connection: close\r\n\r\n"
        var full = Data(header.utf8)
        full.append(body)
        full.withUnsafeBytes { rawBuf in
            guard let base = rawBuf.baseAddress else { return }
            var sent = 0
            while sent < full.count {
                let n = Darwin.send(fd, base.advanced(by: sent), full.count - sent, 0)
                if n <= 0 { break }
                sent += n
            }
        }
    }

    private func guessContentType(_ path: String) -> String {
        let lower = path.lowercased()
        if lower.hasSuffix(".html") { return "text/html; charset=utf-8" }
        if lower.hasSuffix(".js") { return "application/javascript; charset=utf-8" }
        if lower.hasSuffix(".webmanifest") || lower.hasSuffix(".json") { return "application/manifest+json; charset=utf-8" }
        if lower.hasSuffix(".png") { return "image/png" }
        if lower.hasSuffix(".css") { return "text/css; charset=utf-8" }
        if lower.hasSuffix(".svg") { return "image/svg+xml" }
        return "application/octet-stream"
    }
}
