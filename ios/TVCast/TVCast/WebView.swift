import SwiftUI
import WebKit
import AVFoundation
import MediaPlayer
import Network

enum AppConfig {
    /// Your TV Cast web app. Change this if you move it to a different address.
    static let url = URL(string: "https://ryancola4-alt.github.io/tvcast/")!
}

struct WebView: UIViewRepresentable {
    let url: URL

    func makeCoordinator() -> Coordinator { Coordinator(home: url) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true                 // play inside the app, not the system player
        config.mediaTypesRequiringUserActionForPlayback = []    // start picture + sound right away
        config.allowsPictureInPictureMediaPlayback = true
        config.allowsAirPlayForMediaPlayback = true
        config.websiteDataStore = .default()                    // remembers your TV, code and remote layout
        config.applicationNameForUserAgent = "TVCastApp/1.1"    // lets the page know it's inside the app
        if #available(iOS 16.4, *) {
            config.preferences.isElementFullscreenEnabled = true  // the page's full-screen button works for real
        }
        // The page talks to the app through window.webkit.messageHandlers.tvcast (taps, volume buttons).
        config.userContentController.add(context.coordinator, name: "tvcast")

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.backgroundColor = .black
        webView.scrollView.contentInsetAdjustmentBehavior = .never   // page handles the notch itself
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false
        webView.allowsBackForwardNavigationGestures = false
        webView.allowsLinkPreview = false
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        if #available(iOS 16.4, *) {
            webView.isInspectable = true    // lets Safari on a Mac inspect the app if something needs debugging
        }
        context.coordinator.webView = webView
        context.coordinator.volumeKeys.attach(to: webView)
        // Always ask GitHub for the newest page, so web updates show up on the next launch.
        webView.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 20))

        let nc = NotificationCenter.default
        nc.addObserver(context.coordinator, selector: #selector(Coordinator.didBecomeActive),
                       name: UIApplication.didBecomeActiveNotification, object: nil)
        nc.addObserver(context.coordinator, selector: #selector(Coordinator.willResignActive),
                       name: UIApplication.willResignActiveNotification, object: nil)
        nc.addObserver(context.coordinator, selector: #selector(Coordinator.postureChanged(_:)),
                       name: .tvcastPosture, object: nil)
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let home: URL
        weak var webView: WKWebView?
        private var showingOffline = false
        private var folded = false
        let volumeKeys = VolumeKeys()
        private let lightTap = UIImpactFeedbackGenerator(style: .light)
        private let mediumTap = UIImpactFeedbackGenerator(style: .medium)
        private let netMonitor = NWPathMonitor()
        private var netType = ""

        init(home: URL) {
            self.home = home
            super.init()
            volumeKeys.onPress = { [weak self] up in
                self?.webView?.evaluateJavaScript("window.tvcastVolumeKey && window.tvcastVolumeKey('\(up ? "up" : "down")')",
                                                  completionHandler: nil)
            }
            lightTap.prepare()
            // Wi-Fi or cellular? The page uses this to switch the light "data saver" picture on and off.
            netMonitor.pathUpdateHandler = { [weak self] path in
                let type = path.usesInterfaceType(.wifi) || path.usesInterfaceType(.wiredEthernet) ? "wifi"
                         : path.usesInterfaceType(.cellular) ? "cellular" : ""
                DispatchQueue.main.async {
                    guard let self, type != self.netType else { return }
                    self.netType = type
                    self.sendNet()
                }
            }
            netMonitor.start(queue: DispatchQueue(label: "tvcast.net"))
        }

        private func sendNet() {
            webView?.evaluateJavaScript("window.tvcastNet && window.tvcastNet('\(netType)')", completionHandler: nil)
        }

        // MARK: messages from the page

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
            switch type {
            case "haptic":
                let gen = (body["strength"] as? String) == "medium" ? mediumTap : lightTap
                gen.impactOccurred()
                gen.prepare()
            case "volumeKeys":
                volumeKeys.wanted = (body["on"] as? Bool) ?? false
            default:
                break
            }
        }

        // MARK: iPhone Duo fold

        @objc func postureChanged(_ note: Notification) {
            guard let isFolded = note.object as? Bool else { return }
            folded = isFolded
            sendPosture()
        }

        private func sendPosture() {
            webView?.evaluateJavaScript("window.tvcastPosture && window.tvcastPosture('\(folded ? "folded" : "flat")')",
                                        completionHandler: nil)
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            sendPosture()   // a freshly loaded page needs to know how the phone is folded
            sendNet()       // ...and whether it's on Wi-Fi or cellular
        }

        // Coming back to the app: tell the page to check its connection right away.
        @objc func didBecomeActive() {
            volumeKeys.resume()
            if showingOffline { retry(); return }
            webView?.evaluateJavaScript("window.dispatchEvent(new Event('online'))", completionHandler: nil)
        }

        // Leaving the app: let the page float the video in Picture in Picture.
        @objc func willResignActive() {
            volumeKeys.pause()
            webView?.evaluateJavaScript("window.tvcastEnterPiP && window.tvcastEnterPiP()", completionHandler: nil)
        }

        func retry() {
            showingOffline = false
            webView?.load(URLRequest(url: home, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 20))
        }

        // MARK: navigation

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.allow) }
            if url.scheme == "tvcast", url.host == "retry" {
                retry(); return decisionHandler(.cancel)
            }
            // Links to other sites open in Safari; the app itself stays put.
            if action.navigationType == .linkActivated, url.host != home.host {
                UIApplication.shared.open(url)
                return decisionHandler(.cancel)
            }
            decisionHandler(.allow)
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { UIApplication.shared.open(url) }
            return nil
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            showOffline(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            if (error as NSError).code != NSURLErrorCancelled { showOffline(error) }
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            webView.reload()    // iOS reclaimed memory in the background; just start again
        }

        private func showOffline(_ error: Error) {
            guard (error as NSError).code != NSURLErrorCancelled else { return }
            showingOffline = true
            let html = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
            <style>
              html,body{margin:0;height:100%;background:#000;color:#e9ecf2;font:17px -apple-system,system-ui,sans-serif}
              main{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:24px;text-align:center}
              p{color:#8a92a6;margin:0;max-width:300px;line-height:1.4}
              a{margin-top:10px;padding:13px 26px;border-radius:14px;background:#8fb4ff;color:#090b12;font-weight:600;text-decoration:none}
            </style></head><body><main>
              <strong>Can't reach TV Cast</strong>
              <p>Check that this iPhone is online, then try again.</p>
              <a href="tvcast://retry">Try again</a>
            </main></body></html>
            """
            webView?.loadHTMLString(html, baseURL: nil)
        }

        // MARK: JavaScript alert / confirm / prompt (used by "Forget this TV" and copy-link fallback)

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            present(alert) { completionHandler() }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            present(alert) { completionHandler(false) }
        }

        func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                     defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                     completionHandler: @escaping (String?) -> Void) {
            let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
            alert.addTextField { $0.text = defaultText }
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(alert.textFields?.first?.text) })
            present(alert) { completionHandler(nil) }
        }

        /// Shows an alert on top of whatever is on screen; calls `fallback` if there's nowhere to show it.
        private func present(_ alert: UIAlertController, fallback: @escaping () -> Void) {
            var top = webView?.window?.rootViewController
            while let next = top?.presentedViewController { top = next }
            guard let host = top else { fallback(); return }
            host.present(alert, animated: true)
        }
    }
}

/// Turns the phone's volume buttons into the TV remote's VOL +/- while TV Cast is open.
/// iOS has no direct "volume button" event, so this watches the system volume, reports which way
/// it moved, and puts it straight back (a hidden volume view keeps the volume pop-up from showing).
final class VolumeKeys {
    var onPress: ((Bool) -> Void)?
    /// Set by the page's "Volume buttons: TV / phone" menu option.
    var wanted = false { didSet { wanted ? start() : stop() } }

    private let volumeView = MPVolumeView(frame: CGRect(x: -1000, y: -1000, width: 20, height: 20))
    private var observation: NSKeyValueObservation?
    private var baseline: Float = 0.5
    private var ignoreUntil = Date.distantPast

    func attach(to view: UIView) {
        volumeView.alpha = 0.01
        volumeView.isUserInteractionEnabled = false
        view.addSubview(volumeView)
    }

    func pause() { stop() }
    func resume() { if wanted { start() } }

    private var slider: UISlider? {
        volumeView.subviews.compactMap { $0 as? UISlider }.first
    }

    private func start() {
        guard observation == nil else { return }
        let session = AVAudioSession.sharedInstance()
        try? session.setActive(true)
        baseline = session.outputVolume
        // At the very top or bottom a press doesn't change the volume, so start from the middle.
        if baseline < 0.1 || baseline > 0.9 {
            baseline = 0.5
            setVolume(baseline)
        }
        observation = session.observe(\.outputVolume, options: [.new]) { [weak self] _, change in
            guard let value = change.newValue else { return }
            DispatchQueue.main.async { self?.volumeChanged(to: value) }
        }
    }

    private func stop() {
        observation?.invalidate()
        observation = nil
    }

    private func volumeChanged(to value: Float) {
        if Date() < ignoreUntil { return }            // our own reset, not a button press
        if abs(value - baseline) < 0.001 { return }
        onPress?(value > baseline)
        setVolume(baseline)
    }

    private func setVolume(_ value: Float) {
        ignoreUntil = Date().addingTimeInterval(0.15)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.01) { [weak self] in
            self?.slider?.value = value
        }
    }
}
