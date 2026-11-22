import UIKit
import WebKit
import LocalAuthentication
import Security

final class ViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var webView: WKWebView!
    private var assetServer: LocalAssetServer?
    private var privacyShieldView: UIVisualEffectView?
    private let keychainService = "com.synapsetw.ios.sandbox.vault"

    override var preferredStatusBarStyle: UIStatusBarStyle {
        return .lightContent
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 15.0 / 255.0, green: 23.0 / 255.0, blue: 42.0 / 255.0, alpha: 1.0)

        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        config.userContentController.add(self, name: "shlAuth")

        let bridgeJS = """
        window.__SYNAPSE_IOS_APP__ = true;
        window.__SYNAPSE_MOBILE_PROXY__ = true;
        (function() {
          const pending = new Map();
          let seq = 1;
          window.__shlNativeResolve = function(id, payload) {
            const p = pending.get(id);
            if (!p) return;
            pending.delete(id);
            p.resolve(payload);
          };
          function callNative(action, params) {
            return new Promise((resolve) => {
              const id = String(seq++);
              pending.set(id, { resolve });
              try {
                window.webkit.messageHandlers.shlAuth.postMessage({ id, action, params: params || {} });
              } catch (e) {
                pending.delete(id);
                resolve({ ok: false, error: String(e && e.message ? e.message : e) });
              }
            });
          }
          window.__SHL_IOS_BRIDGE__ = {
            canEvaluateAuth: () => callNative('canEvaluateAuth', {}),
            authenticate: (reason) => callNative('authenticate', { reason: reason || '驗證身分以解鎖 SHL TW 本機沙盒保險箱' }),
            keychainSet: (key, value) => callNative('keychainSet', { key, value }),
            keychainGet: (key) => callNative('keychainGet', { key }),
            keychainDelete: (key) => callNative('keychainDelete', { key })
          };
        })();
        """

        let initScript = WKUserScript(
            source: bridgeJS,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        config.userContentController.addUserScript(initScript)

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.bounces = false
        webView.backgroundColor = view.backgroundColor
        webView.isOpaque = false
        webView.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor)
        ])

        setupLifecycleObservers()
        startLocalServerAndLoad()
    }

    private func setupLifecycleObservers() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleWillResignActive),
            name: UIApplication.willResignActiveNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleDidBecomeActive),
            name: UIApplication.didBecomeActiveNotification,
            object: nil
        )
    }

    @objc private func handleWillResignActive() {
        showPrivacyShield()
        webView?.evaluateJavaScript("if (typeof window.__shlOnNativeBackground === 'function') window.__shlOnNativeBackground();", completionHandler: nil)
    }

    @objc private func handleDidBecomeActive() {
        hidePrivacyShield()
        webView?.evaluateJavaScript("if (typeof window.__shlOnNativeResume === 'function') window.__shlOnNativeResume();", completionHandler: nil)
    }

    private func showPrivacyShield() {
        guard privacyShieldView == nil else { return }
        let blur = UIBlurEffect(style: .dark)
        let shield = UIVisualEffectView(effect: blur)
        shield.frame = view.bounds
        shield.autoresizingMask = [.flexibleWidth, .flexibleHeight]

        let label = UILabel()
        label.text = "SHL TW 安全沙盒保護中"
        label.textColor = UIColor.white.withAlphaComponent(0.92)
        label.font = UIFont.systemFont(ofSize: 18, weight: .bold)
        label.translatesAutoresizingMaskIntoConstraints = false
        shield.contentView.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: shield.contentView.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: shield.contentView.centerYAnchor)
        ])

        view.addSubview(shield)
        privacyShieldView = shield
    }

    private func hidePrivacyShield() {
        privacyShieldView?.removeFromSuperview()
        privacyShieldView = nil
    }

    private func startLocalServerAndLoad() {
        guard let webRoot = Bundle.main.resourceURL?.appendingPathComponent("WebAssets") else {
            return
        }
        let server = LocalAssetServer(assetsRoot: webRoot)
        if let port = server.start(preferredPort: 18432) {
            self.assetServer = server
            if let url = URL(string: "http://127.0.0.1:\(port)/index.html") {
                webView.load(URLRequest(url: url))
                return
            }
        }
        let fallback = webRoot.appendingPathComponent("index.html")
        webView.loadFileURL(fallback, allowingReadAccessTo: webRoot)
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        assetServer?.stop()
    }

    // MARK: - WKScriptMessageHandler (Face ID / Touch ID / Device Passcode + Sandboxed Keychain)
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "shlAuth",
              let body = message.body as? [String: Any],
              let reqId = body["id"] as? String,
              let action = body["action"] as? String else {
            return
        }
        let params = body["params"] as? [String: Any] ?? [:]

        switch action {
        case "canEvaluateAuth":
            let ctx = LAContext()
            var authError: NSError?
            let available = ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &authError)
            let biometry: String
            switch ctx.biometryType {
            case .faceID:
                biometry = "faceID"
            case .touchID:
                biometry = "touchID"
            default:
                biometry = "passcode"
            }
            resolveJS(id: reqId, payload: [
                "ok": true,
                "available": available,
                "biometryType": biometry
            ])

        case "authenticate":
            let reason = (params["reason"] as? String) ?? "驗證身分以解鎖 SHL TW 保險箱"
            let ctx = LAContext()
            ctx.localizedFallbackTitle = "輸入手機解鎖密碼"
            var authError: NSError?
            guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &authError) else {
                resolveJS(id: reqId, payload: [
                    "ok": false,
                    "error": authError?.localizedDescription ?? "此裝置未設定 Face ID / Touch ID 或解鎖密碼"
                ])
                return
            }
            let biometry: String
            switch ctx.biometryType {
            case .faceID:
                biometry = "faceID"
            case .touchID:
                biometry = "touchID"
            default:
                biometry = "passcode"
            }
            ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { [weak self] success, evalError in
                DispatchQueue.main.async {
                    if success {
                        self?.resolveJS(id: reqId, payload: [
                            "ok": true,
                            "biometryType": biometry
                        ])
                    } else {
                        self?.resolveJS(id: reqId, payload: [
                            "ok": false,
                            "error": evalError?.localizedDescription ?? "身分驗證已取消或失敗"
                        ])
                    }
                }
            }

        case "keychainSet":
            guard let key = params["key"] as? String,
                  let value = params["value"] as? String,
                  let data = value.data(using: .utf8) else {
                resolveJS(id: reqId, payload: ["ok": false, "error": "invalid_params"])
                return
            }
            let status = keychainWrite(account: key, data: data)
            resolveJS(id: reqId, payload: ["ok": status == errSecSuccess, "status": Int(status)])

        case "keychainGet":
            guard let key = params["key"] as? String else {
                resolveJS(id: reqId, payload: ["ok": false, "error": "invalid_params"])
                return
            }
            if let value = keychainRead(account: key) {
                resolveJS(id: reqId, payload: ["ok": true, "value": value])
            } else {
                resolveJS(id: reqId, payload: ["ok": false, "notFound": true])
            }

        case "keychainDelete":
            guard let key = params["key"] as? String else {
                resolveJS(id: reqId, payload: ["ok": false, "error": "invalid_params"])
                return
            }
            let status = keychainDelete(account: key)
            resolveJS(id: reqId, payload: ["ok": status == errSecSuccess || status == errSecItemNotFound])

        default:
            resolveJS(id: reqId, payload: ["ok": false, "error": "unknown_action"])
        }
    }

    private func resolveJS(id: String, payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
              let jsonStr = String(data: data, encoding: .utf8) else {
            return
        }
        let escapedId = id.replacingOccurrences(of: "'", with: "")
        let js = "window.__shlNativeResolve && window.__shlNativeResolve('\(escapedId)', \(jsonStr));"
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    // MARK: - iOS App-Sandboxed Keychain (ThisDeviceOnly — never exported or shared across apps)
    private func keychainWrite(account: String, data: Data) -> OSStatus {
        _ = keychainDelete(account: account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        ]
        return SecItemAdd(query as CFDictionary, nil)
    }

    private func keychainRead(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    private func keychainDelete(account: String) -> OSStatus {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account
        ]
        return SecItemDelete(query as CFDictionary)
    }

    // MARK: - WKNavigationDelegate
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated {
            let host = url.host ?? ""
            let isInternal = host == "127.0.0.1"
                || host == "localhost"
                || host.hasPrefix("192.168.")
                || host.hasPrefix("10.")
                || host.contains("tradingview.com")
            if !isInternal && (url.scheme == "http" || url.scheme == "https") {
                UIApplication.shared.open(url, options: [:], completionHandler: nil)
                decisionHandler(.cancel)
                return
            }
        }
        decisionHandler(.allow)
    }

    // MARK: - WKUIDelegate (Native iOS Alert/Confirm/Prompt Dialogs)
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = UIAlertController(title: "SHL TW", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "確定", style: .default) { _ in completionHandler() })
        present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = UIAlertController(title: "SHL TW", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "確認", style: .default) { _ in completionHandler(true) })
        present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = UIAlertController(title: "SHL TW", message: prompt, preferredStyle: .alert)
        alert.addTextField { tf in tf.text = defaultText }
        alert.addAction(UIAlertAction(title: "取消", style: .cancel) { _ in completionHandler(nil) })
        alert.addAction(UIAlertAction(title: "確定", style: .default) { _ in
            completionHandler(alert.textFields?.first?.text)
        })
        present(alert, animated: true)
    }
}
