package local.thesaint.app;

import android.app.Activity;
import android.content.Intent;
import android.content.ClipData;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.*;
import android.view.View;
import android.view.WindowInsets;
import java.io.ByteArrayInputStream;
import java.util.Collections;

public final class MainActivity extends Activity {
    private WebView web;
    private ValueCallback<Uri[]> picker;
    private final SaintData data = new SaintData();
    private static final String ORIGIN = "https://appassets.androidplatform.net";

    @Override public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        web.setBackgroundColor(Color.rgb(247,246,240));
        android.widget.FrameLayout container = new android.widget.FrameLayout(this);
        container.setBackgroundColor(Color.rgb(247,246,240));
        container.addView(web, new android.widget.FrameLayout.LayoutParams(-1, -1));
        container.setOnApplyWindowInsetsListener((view, insets) -> {
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                                insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(false);
        settings.setAllowFileAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        WebView.setWebContentsDebuggingEnabled(true); // Development APK only.
        web.addJavascriptInterface(new Bridge(), "SaintAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return !request.getUrl().toString().equals(ORIGIN + "/");
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                String path = request.getUrl().getPath();
                String filename = "/".equals(path) ? "index.html" : "/app.js".equals(path) ? "app.js" : "/style.css".equals(path) ? "style.css" : null;
                try {
                    if (!"https".equals(request.getUrl().getScheme()) || !"appassets.androidplatform.net".equals(request.getUrl().getHost()) || filename == null) {
                        return new WebResourceResponse("text/plain", "utf-8", 404, "Not found", Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
                    }
                    String mime = filename.endsWith("js") ? "text/javascript" : filename.endsWith("css") ? "text/css" : "text/html";
                    java.util.Map<String,String> headers = new java.util.HashMap<>();
                    headers.put("Cache-Control", "no-store");
                    headers.put("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'");
                    return new WebResourceResponse(mime,"utf-8",200,"OK",headers,getAssets().open(filename));
                } catch (Exception error) {
                    return new WebResourceResponse("text/plain", "utf-8", new ByteArrayInputStream(new byte[0]));
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (picker != null) picker.onReceiveValue(null);
                picker = callback;
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try { startActivityForResult(intent, 1); }
                catch (android.content.ActivityNotFoundException error) { picker.onReceiveValue(null); picker = null; }
                return true;
            }
        });
        setContentView(container);
        container.requestApplyInsets();
        web.loadUrl(ORIGIN + "/");
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent result) {
        super.onActivityResult(requestCode, resultCode, result);
        if (requestCode != 1 || picker == null) return;
        Uri[] uris = null;
        if (resultCode == RESULT_OK && result != null) {
            ClipData clip = result.getClipData();
            if (clip != null) {
                uris = new Uri[clip.getItemCount()];
                for (int i=0; i<uris.length; i++) uris[i] = clip.getItemAt(i).getUri();
            } else if (result.getData() != null) uris = new Uri[]{result.getData()};
        }
        picker.onReceiveValue(uris);
        picker = null;
    }

    @Override protected void onDestroy() {
        if (picker != null) picker.onReceiveValue(null);
        web.removeJavascriptInterface("SaintAndroid");
        web.destroy();
        super.onDestroy();
    }

    private final class Bridge {
        @JavascriptInterface public String request(String path, String payload) {
            return data.request(path, payload);
        }
    }
}
