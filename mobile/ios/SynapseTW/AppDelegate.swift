import UIKit

@main
class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        let win = UIWindow(frame: UIScreen.main.bounds)
        win.backgroundColor = UIColor(red: 15.0 / 255.0, green: 23.0 / 255.0, blue: 42.0 / 255.0, alpha: 1.0)
        win.rootViewController = ViewController()
        win.makeKeyAndVisible()
        self.window = win
        return true
    }
}
