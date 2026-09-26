const DISCLOSURE: &str = "<img aria-hidden=\"true\" class=\"disclosure\" src=\"/assets/disclosure-26d63471.svg\" width=\"10\" height=\"10\" />";
const SWITCH: &str = "<img alt=\"the switch\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" />";
const GEAR: &str = "<img aria-hidden=\"true\" src=\"/assets/external/gear-50e77d2b.svg\" width=\"20\" height=\"20\" />";

struct Platform<'a> {
    browser: &'a str,
    system: &'a str,
    ios: bool,
    android: bool,
    windows: bool,
}

impl Platform<'_> {
    fn desktop(&self) -> bool {
        !self.ios && !self.android
    }
}

fn platform(agent: &str) -> Platform<'_> {
    let ios = agent.contains("iPhone") || agent.contains("iPad");
    let android = agent.contains("Android");
    let windows = agent.contains("Windows");
    let browser = if agent.contains("Edge/") {
        "Edge"
    } else if ios && agent.contains("FxiOS/") {
        "Safari"
    } else if agent.contains("Firefox/") || agent.contains("FxiOS/") {
        "Firefox"
    } else if agent.contains("Chrome/") || agent.contains("CriOS/") {
        "Chrome"
    } else if agent.contains("Safari/") {
        "Safari"
    } else {
        "Mozilla"
    };
    let system = if android {
        "Android"
    } else if agent.contains("iPad") {
        "iPad"
    } else if agent.contains("iPhone") {
        "iPhone"
    } else if agent.contains("Macintosh") {
        "macOS"
    } else if windows {
        "Windows"
    } else if agent.contains("CrOS") {
        "ChromeOS"
    } else if agent.contains("Linux") {
        "Linux"
    } else {
        "system"
    };
    Platform {
        browser,
        system,
        ios,
        android,
        windows,
    }
}

fn details(class: &str, attributes: &str, icon: &str, title: &str, body: &str) -> String {
    format!(
        "<details class=\"{class}\" {attributes}><summary class=\"btn\">{icon}<strong>{title}</strong>{DISCLOSURE}</summary>{body}</details>"
    )
}

fn browser_settings(platform: &Platform<'_>, root_url: &str) -> String {
    if platform.ios && matches!(platform.browser, "Safari" | "Chrome") {
        return String::new();
    }
    let body = match (platform.browser, platform.android, platform.desktop()) {
        ("Firefox", true, _) => "<ol><li>Tap <em><img alt=\"the View site information button\" src=\"/assets/lock-7cdf4b3d.svg\" width=\"20\" height=\"20\" /></em> in the address bar.</li><li>Tap <em>Notification</em> to change to <em>Allowed</em>.</li></ol>".to_string(),
        ("Edge", _, true) => {
            let system_steps = if platform.windows {
                format!("<li>Click <em>Start</em>, then <em>Settings</em>.</li><li>Go to <em>System &gt; Notification</em>.</li><li>Click <em>{SWITCH}</em> <em>ON</em> for Edge.</li>")
            } else {
                format!("<li>Click <em aria-label=\"the Apple menu\"></em> in the top left.</li><li>Click <em>System Settings…</em>.</li><li>Click <em>Notifications</em>.</li><li>Click <em>Edge</em>.</li><li>Click <em>{SWITCH}</em> to <em>Allow notifications</em>.</li>")
            };
            format!("<h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for this website.</h2><ol><li>Click <em><img alt=\"the View site information button\" src=\"/assets/lock-7cdf4b3d.svg\" width=\"20\" height=\"20\" /></em> left of the address bar.</li><li>Under <em>Permissions for this site &gt; Notifications</em>, choose <em>Allow</em>.</li></ol><h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for Edge.</h2><ol>{system_steps}</ol>")
        }
        ("Firefox", _, true) => {
            let system_steps = if platform.windows {
                format!("<li>Click <em>Start</em>, then <em>Settings</em>.</li><li>Go to <em>System &gt; Notification</em>.</li><li>Click <em><img alt=\"the toggle button\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> <em>ON</em> for Firefox.</li>")
            } else {
                format!("<li>Click <em aria-label=\"the Apple menu\"></em> in the top left.</li><li>Click <em>System Settings…</em>.</li><li>Click <em>Notifications</em>.</li><li>Click <em>Firefox</em>.</li><li>Click <em>{SWITCH}</em> to <em>Allow notifications</em>.</li>")
            };
            format!("<h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for this website.</h2><ol><li>Click <em>Firefox</em> in the top left.</li><li>Click <em>Settings…</em>.</li><li>Click <em>Privacy &amp; Security</em> in the sidebar.</li><li>Scroll down to <em>Permissions</em>.</li><li>Click <em>Settings</em> next to <em>Notifications</em>.</li><li>Select <em>Allow</em> next to <em>{root_url}</em>.</li></ol><h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for Firefox.</h2><ol>{system_steps}</ol>")
        }
        ("Chrome", _, true) => {
            let system_steps = if platform.windows {
                format!("<li>Click <em>Start</em>, then <em>Settings</em>.</li><li>Go to <em>System &gt; Notification</em>.</li><li>Click <em>{SWITCH}</em> <em>ON</em> for Chrome.</li>")
            } else {
                format!("<li>Click <em aria-label=\"the Apple menu\"></em> in the top left.</li><li>Click <em>System Settings…</em>.</li><li>Click <em>Notifications</em>.</li><li>Click <em>Chrome</em>.</li><li>Click <em>{SWITCH}</em> to <em>Allow notifications</em>.</li>")
            };
            format!("<h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for this website.</h2><ol><li>Click the <em><img alt=\"View site information\" src=\"/assets/external/sliders-3979a007.svg\" width=\"20\" height=\"20\" /></em> icon in the address bar.</li><li>Click <em>Site Settings</em>.</li><li>Ensure notifications are <em>Allowed</em>.</li></ol><h2 class=\"txt-normal txt-medium margin-block-start\">Turn on notifications for Chrome.</h2><ol>{system_steps}</ol>")
        }
        ("Chrome", true, _) => format!("<ol><li>Tap the <em><img alt=\"More options\" src=\"/assets/menu-dots-vertical-c247e3cc.svg\" width=\"16\" height=\"16\" /></em> menu button.</li><li>Tap <em>Settings</em>.</li><li>Tap <em>Notifications</em>.</li><li>Tap <em>{SWITCH}</em> to <em>Allow Chrome notifications</em>.</li><li>Tap <em>{SWITCH}</em> next to <em>Web apps</em>.</li><li>Tap <em><img alt=\"the notification bell\" src=\"/assets/notification-bell-alert-b467cde7.svg\" width=\"16\" height=\"16\" /></em> and select <em>Allow</em>.</li></ol>"),
        ("Safari", _, true) => format!("<ol><li>Click <em>Safari</em> in the top left.</li><li>Click <em>Settings…</em>.</li><li>Click the <em>Websites</em> tab.</li><li>Click <em>Notifications</em> in the sidebar.</li><li>Click <em>{root_url}</em> in the list.</li><li>Select <em>Allow</em>.</li></ol>"),
        _ => format!("<p>Ensure notifications are enabled for <em>{root_url}</em> in your web browser settings.</p>"),
    };
    details(
        "notifications-help",
        "data-notifications-target=\"details\"",
        "<img aria-hidden=\"true\" src=\"/assets/external/web-24ffe636.svg\" width=\"20\" height=\"20\" />",
        &format!("Check your {} settings", platform.browser),
        &body,
    )
}

fn system_settings(platform: &Platform<'_>) -> String {
    let body = match (platform.browser, platform.android, platform.desktop()) {
        ("Firefox", true, _) => "<ol><li>Tap the <em><img alt=\"More options\" src=\"/assets/menu-dots-vertical-c247e3cc.svg\" width=\"16\" height=\"16\" /></em> menu button.</li><li>Tap <em>Settings</em>.</li><li>Tap <em>Notifications</em>.</li><li>Tap <em><img alt=\"the toggle button\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> to <em>Allow Firefox notifications</em>.</li></ol>".to_string(),
        ("Edge", _, true) => "<ol><li>Click <em>Start</em>, then <em>Settings</em>.</li><li>Go to <em>System &gt; Notification</em>.</li><li>Click <em><img alt=\"the toggle button\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> <em>ON</em> for Rustfire.</li></ol>".to_string(),
        ("Firefox" | "Chrome", _, true) if platform.windows => "<ol><li>Click <em>Start</em>, then <em>Settings</em>.</li><li>Go to <em>System &gt; Notification</em>.</li><li>Click <em><img alt=\"the toggle button\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> <em>ON</em> for Rustfire.</li></ol>".to_string(),
        ("Firefox" | "Chrome" | "Safari", _, true) => "<ol><li>Click <em aria-label=\"the Apple menu\"></em> in the top left.</li><li>Click <em>System Settings…</em>.</li><li>Click <em>Notifications</em>.</li><li>Click <em>Rustfire</em>.</li><li>Click <em><img alt=\"the allow notifications switch\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> to <em>Allow notifications</em>.</li></ol>".to_string(),
        ("Safari" | "Chrome", _, _) if platform.ios => format!("<ol><li>Open the <em>{GEAR}</em> Settings app.</li><li>Scroll to and tap <em>Rustfire</em>.</li><li>Tap <em>Notifications</em>.</li><li>Tap <em><img alt=\"the allow notifications switch button\" src=\"/assets/external/switch-eec22a2d.svg\" width=\"22\" height=\"22\" /></em> to <em>Allow Notifications</em>.</li></ol>"),
        ("Chrome", true, _) => format!("<ol><li>Open the <em>{GEAR}</em> Settings app.</li><li>Tap <em>Notifications</em>.</li><li>Tap <em>App notifications</em>.</li><li>Scroll to <em>Rustfire</em>.</li><li>Tap <em>{SWITCH}</em> to <em>Allow Notifications</em>.</li></ol>"),
        _ => format!("<p>Ensure notifications are allowed for {} in your system settings.</p>", platform.browser),
    };
    details(
        "notifications-help hide-in-browser",
        "data-notifications-target=\"details\"",
        GEAR,
        &format!("Check your {} settings", platform.system),
        &body,
    )
}

fn install_instructions(platform: &Platform<'_>) -> String {
    if platform.browser == "Chrome" || (platform.browser == "Firefox" && !platform.android) {
        return String::new();
    }
    let body = match (platform.browser, platform.android, platform.ios, platform.desktop()) {
        ("Edge", _, _, _) => "<ol><li>Click <em><img alt=\"the app available - install Rustfire chat button\" src=\"/assets/external/install-edge-0b7cd918.svg\" width=\"16\" height=\"16\" /></em>in the address bar.</li><li>Click <em>Install</em>.</li></ol>".to_string(),
        ("Chrome", true, _, _) => "<ol><li>Tap the <em><img alt=\"More options\" src=\"/assets/menu-dots-vertical-c247e3cc.svg\" width=\"16\" height=\"16\" /></em> menu button.</li><li>Tap <em>Install app</em> in the menu.</li></ol>".to_string(),
        ("Firefox", true, _, _) => "<ol><li>Tap the <em><img alt=\"More options\" src=\"/assets/menu-dots-vertical-c247e3cc.svg\" width=\"16\" height=\"16\" /></em> menu button.</li><li>Tap <em>Install</em> in the menu.</li></ol>".to_string(),
        ("Safari", _, _, true) => "<ol><li>Click <em>File</em> in the top left.</li><li>Click <em>Add to Dock…</em>.</li></ol>".to_string(),
        ("Safari" | "Chrome", _, true, _) => format!("<p>To receive push notifications in {} for {}, you must install Rustfire as a web app.</p><ol><li>Tap <em><img alt=\"the share button\" src=\"/assets/external/share-f9d3e998.svg\" width=\"20\" height=\"20\" /></em></li><li>Tap <em>Add to Home Screen</em>.</li></ol>", platform.browser, platform.system),
        _ => "<p>Some platforms require you to install Rustfire as a web app to receive push notifications.</p>".to_string(),
    };
    let installer = "<div class=\"margin-block-start txt-align-center pwa__installer\"><hr class=\"separator margin-block\"><button class=\"btn btn--reversed center\" data-action=\"pwa-install#promptInstall\"><img aria-hidden=\"true\" src=\"/assets/external/install-f762b3be.svg\" />Install now</button></div>";
    details(
        "notifications-help pwa__instructions hide-in-pwa",
        "data-controller=\"pwa-install\" data-pwa-install-prompting-class=\"pwa--can-install\" data-notifications-target=\"details\"",
        "<img aria-hidden=\"true\" src=\"/assets/external/install-f762b3be.svg\" width=\"20\" height=\"20\" />",
        "Install Rustfire as a web app.",
        &(body + installer),
    )
}

pub fn render(agent: &str, root_url: &str) -> String {
    let platform = platform(agent);
    format!(
        "{}{}{}",
        browser_settings(&platform, root_url),
        system_settings(&platform),
        install_instructions(&platform)
    )
}
