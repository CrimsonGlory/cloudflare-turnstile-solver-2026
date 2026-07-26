// Object map for proxy ID info.
let active_proxy = null;

// Object map for proxy authentication credentials.
let active_credentials = null;

// Load our state from local storage on script wakeup to prevent data loss after idling.
const state_loaded = chrome.storage.local.get(['active_proxy', 'active_credentials']).then((res) => {
    active_proxy = res.active_proxy || null;
    active_credentials = res.active_credentials || null;
    update_chrome_proxy_config();
});

// Chromium service workers suspend when idle. This saves state before the script sleeps.
function save_state() {
    chrome.storage.local.set({ active_proxy, active_credentials });
}

// Prevent WebRTC from leaking the host's local IP address (peeking), without disabling WebRTC itself.
if (chrome.privacy && chrome.privacy.network && chrome.privacy.network.webRTCIPHandlingPolicy) {
    chrome.privacy.network.webRTCIPHandlingPolicy.set({ value: "default_public_interface_only" });
}

// Answer proxy authentication challenges natively with stored credentials.
// FIXED: Using asyncBlocking and awaiting state_loaded to survive Service Worker idle suspensions.
chrome.webRequest.onAuthRequired.addListener(
    (details, callback) => {
        if (!details.isProxy) {
            callback({ cancel: false });
            return;
        }

        // Wait for the storage promise to resolve before processing the credentials.
        state_loaded.then(() => {
            if (active_credentials) {
                callback({
                    authCredentials: {
                        username: active_credentials.username,
                        password: active_credentials.password
                    }
                });
            } else {
                callback({ cancel: false });
            }
        }).catch((err) => {
            console.error("[Proxy Bridge] Error loading credentials:", err);
            callback({ cancel: false });
        });
    },
    { urls: ["<all_urls>"] },
    ["asyncBlocking"]
);

// Update Chromium global proxy configuration using fixed_servers for strict routing.
function update_chrome_proxy_config() {
    if (!active_proxy) {
        chrome.proxy.settings.set({ value: { mode: "system" }, scope: "regular" });
        return;
    }

    const config = {
        mode: "fixed_servers",
        rules: {
            singleProxy: {
                scheme: active_proxy.type,
                host: active_proxy.host,
                port: active_proxy.port
            },
            bypassList: ["localhost", "127.0.0.1"]
        }
    };

    chrome.proxy.settings.set({
        value: config,
        scope: "regular"
    });
}

// Listen to messages posted by the client if we want to set up a proxy.
chrome.runtime.onMessage.addListener((message, sender, send_response) => {
    if (message.action === "setup_proxy" && sender.tab) {
        // Ensure state is loaded before modifying.
        state_loaded.then(() => {
            const proxy_string = message.proxy_details;
            
            try {
                // "protocol://host:port", "protocol://user:pass@host:port"
                const url = new URL(proxy_string);
                let proxy_type = url.protocol.replace(":", "").toLowerCase();
                const host_name = url.hostname;
                let port_num = parseInt(url.port, 10);

                active_proxy = {
                    type: proxy_type,
                    host: host_name,
                    port: port_num
                };

                // Optional proxy auth, parsed as a "protocol://user:pass@host:port" proxy URL.
                if (url.username) {
                    active_credentials = {
                        username: decodeURIComponent(url.username),
                        password: decodeURIComponent(url.password)
                    };
                } else {
                    active_credentials = null;
                };

                save_state();
                update_chrome_proxy_config();
                send_response({ success: true });
            } catch (err) {
                console.error("[Proxy Bridge] Error parsing proxy URL:", err);
                send_response({ success: false });
            }
        });
        return true; 
    }
    return false;
});