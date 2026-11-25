package com.synapsetw.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.KeyguardManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import java.io.IOException;

public class MainActivity extends Activity {
    private static final int FILE_CHOOSER_REQ = 4201;
    private static final int DEVICE_AUTH_REQ = 4202;

    private WebView webView;
    private LocalAssetServer assetServer;
    private ValueCallback<Uri[]> filePathCallback;

    private class AndroidAuthBridge {
        @JavascriptInterface
        public boolean isDeviceSecure() {
            try {
                KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
                return km != null && km.isDeviceSecure();
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public void requestDeviceUnlock(final String title, final String subtitle) {
            runOnUiThread(() -> {
                try {
                    KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
                    if (km != null && km.isDeviceSecure()) {
                        Intent intent = km.createConfirmDeviceCredentialIntent(
                                title != null && !title.isEmpty() ? title : "SHL TW 身分驗證",
                                subtitle != null && !subtitle.isEmpty() ? subtitle : "請使用人臉辨識、指紋或手機解鎖密碼登入"
                        );
                        if (intent != null) {
                            startActivityForResult(intent, DEVICE_AUTH_REQ);
                            return;
                        }
                    }
                    notifyAuthResult(false, "裝置尚未啟用系統螢幕鎖");
                } catch (Exception e) {
                    notifyAuthResult(false, e.getMessage() != null ? e.getMessage() : "驗證呼叫失敗");
                }
            });
        }
    }

    private void notifyAuthResult(final boolean ok, final String msg) {
        if (webView == null) return;
        final String safeMsg = msg == null ? "" : msg.replace("\\", "\\\\").replace("'", "\\'");
        webView.post(() -> {
            if (webView != null) {
                webView.evaluateJavascript(
                        "if (typeof window.__shlOnAndroidAuthResult === 'function') window.__shlOnAndroidAuthResult(" + ok + ", '" + safeMsg + "');",
                        null
                );
            }
        });
    }

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        setContentView(webView);
        webView.addJavascriptInterface(new AndroidAuthBridge(), "SHLAndroidAuth");

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        settings.setMediaPlaybackRequiresUserGesture(false);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (uri == null) return false;
                String host = uri.getHost() == null ? "" : uri.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)
                        || host.startsWith("192.168.") || host.startsWith("10.")
                        || host.contains("tradingview.com")) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    return true;
                } catch (Exception ignored) {
                    return false;
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                view.evaluateJavascript("window.__SYNAPSE_ANDROID_PROXY__ = true; if (typeof updatePwaInstallStatusUI === 'function') updatePwaInstallStatusUI();", null);
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView webView, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    startActivityForResult(intent, FILE_CHOOSER_REQ);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    Toast.makeText(MainActivity.this, "無法開啟檔案選擇器", Toast.LENGTH_SHORT).show();
                    return false;
                }
            }
        });

        assetServer = new LocalAssetServer(getAssets());
        try {
            int port = assetServer.start(18432);
            webView.loadUrl("http://127.0.0.1:" + port + "/index.html");
        } catch (IOException e) {
            Toast.makeText(this, "本機資源啟動失敗，改用離線載入", Toast.LENGTH_LONG).show();
            webView.loadUrl("file:///android_asset/index.html");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_CHOOSER_REQ && filePathCallback != null) {
            Uri[] results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
        } else if (requestCode == DEVICE_AUTH_REQ) {
            notifyAuthResult(resultCode == RESULT_OK, resultCode == RESULT_OK ? "OK" : "已取消或驗證未通過");
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) {
            try {
                webView.evaluateJavascript("if (typeof window.__shlOnNativeBackground === 'function') window.__shlOnNativeBackground();", null);
            } catch (Exception ignored) {}
            webView.onPause();
            webView.pauseTimers();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.resumeTimers();
            webView.onResume();
            try {
                webView.evaluateJavascript("if (typeof window.__shlOnNativeResume === 'function') window.__shlOnNativeResume();", null);
            } catch (Exception ignored) {}
        }
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            finishAndRemoveTask();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            try {
                webView.evaluateJavascript("if (typeof window.__shlAppShutdown === 'function') window.__shlAppShutdown();", null);
                webView.stopLoading();
                webView.loadUrl("about:blank");
                webView.removeAllViews();
                webView.destroy();
            } catch (Exception ignored) {}
            webView = null;
        }
        if (assetServer != null) {
            try {
                assetServer.stop();
            } catch (Exception ignored) {}
            assetServer = null;
        }
        super.onDestroy();
    }
}
