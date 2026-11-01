import SwiftUI
import AVFoundation

/// TV Cast — native iPhone app.
/// A full-screen shell around the TV Cast web app (the same one on GitHub Pages), with the
/// things Safari can't do: no browser bars at all, sound that plays even with the silent
/// switch on, the screen kept awake while watching, and keeping the stream alive when you
/// briefly switch apps.
///
/// Built with the iOS 27.1 SDK it fills the whole iPhone Duo screen (folded, unfolded,
/// sideways and in Split View); the web page lays itself out for whatever size it gets.
@main
struct TVCastApp: App {
    init() {
        // .playback = TV sound plays even when the ring/silent switch is on silent.
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .moviePlayback, options: [])
        try? session.setActive(true)
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @Environment(\.scenePhase) private var phase

    var body: some View {
        WebView(url: AppConfig.url)
            .ignoresSafeArea()
            .background(Color.black.ignoresSafeArea())
            .statusBarHidden(true)
            .persistentSystemOverlays(.hidden)      // fades the home bar while watching
            .preferredColorScheme(.dark)
            .modifier(HingeWatcher())
            .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
            .onChange(of: phase) { newPhase in
                // Keep the screen awake while the app is open; let it sleep otherwise.
                UIApplication.shared.isIdleTimerDisabled = (newPhase == .active)
                if newPhase == .active {
                    try? AVAudioSession.sharedInstance().setActive(true)
                }
            }
    }
}

extension Notification.Name {
    /// Posted with `object: Bool` (true = half-folded) whenever the iPhone Duo's hinge changes.
    static let tvcastPosture = Notification.Name("TVCastPosture")
}

/// iPhone Duo: tell the page when the phone is half-folded (tabletop or book), so it can put the
/// picture on one half and the remote on the other. Does nothing on phones without a hinge.
struct HingeWatcher: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 27.1, *) {
            content.onHingeChange { _, newContext in
                let folded = newContext.hinge?.status == .partiallyOpen
                NotificationCenter.default.post(name: .tvcastPosture, object: folded)
            }
        } else {
            content
        }
    }
}
