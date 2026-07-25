// Object map for proxy ID info.
let tab_proxies = {};
let origin_proxies = {};

// Object map for per-tab/per-origin proxy authentication credentials.
let tab_proxy_credentials = {};
let origin_proxy_credentials = {};
let attached_tabs = new Set();

// Load state from local storage on script wakeup to prevent data loss after idling.
const state_loaded = chrome.storage.local.get([
    'tab_proxies', 'origin_proxies', 'tab_proxy_credentials', 'origin_proxy_credentials'
]).then((res) => {
    tab_proxies = res.tab_proxies || {};
    origin_proxies = res.origin_proxies || {};
    tab_proxy_credentials = res.tab_proxy_credentials || {};
    origin_proxy_credentials = res.origin_proxy_credentials || {};
});

// Chromium service workers can idle. This will save our state before the script idles.
function save_state() {
    chrome.storage.local.set({
        tab_proxies,
        origin_proxies,
        tab_proxy_credentials,
        origin_proxy_credentials
    });
}

// Prevent WebRTC from leaking the host's local IP address (peeking), 
// without disabling WebRTC itself.
if (chrome.privacy && chrome.privacy.network && chrome.privacy.network.webRTCIPHandlingPolicy) {
    chrome.privacy.network.webRTCIPHandlingPolicy.set({ value: "default_public_interface_only" });
}

// Attach CDP debugger session to a tab for proxy and authentication handling.
async function attach_debugger_if_needed(tab_id) {
    if (attached_tabs.has(tab_id)) return;

    try {
        const target = { tabId: tab_id };
        await chrome.debugger.attach(target, "1.3");
        attached_tabs.add(tab_id);

        // Enable Fetch domain to intercept auth challenges.
        await chrome.debugger.sendCommand(target, "Fetch.enable", {
            handleAuthRequests: true
        });

        console.log(`[CDP Bridge] Attached debugger to tab ${tab_id}`);
    } catch (err) {
        console.error(`[CDP Bridge] Failed to attach debugger to tab ${tab_id}:`, err);
    }
}

// Handle CDP events for dynamic authentication.
chrome.debugger.onEvent.addListener(async (source, method, params) => {
    const tab_id = source.tabId;

    // Answer proxy authentication challenges with any stored credentials.
    if (method == "Fetch.authRequired") {
        
        // Wait for state to load from storage if the script is waking up.
        await state_loaded;

        let creds = tab_proxy_credentials[tab_id];
        if (!creds && params.request && params.request.url) {
            try {
                let request_origin = new URL(params.request.url).origin;
                creds = origin_proxy_credentials[request_origin];
            } catch (e) {}
        }

        if (creds) {
            chrome.debugger.sendCommand(source, "Fetch.continueWithAuth", {
                requestId: params.requestId,
                authChallengeResponse: {
                    response: "ProvideCredentials",
                    username: creds.username,
                    password: creds.password
                }
            });
            console.log(`[CDP Bridge] Responded to CDP auth challenge for tab ${tab_id}`);
        } else {
            chrome.debugger.sendCommand(source, "Fetch.continueWithAuth", {
                requestId: params.requestId,
                authChallengeResponse: { response: "Default" }
            });
        }
    }
});

// Update global proxy configuration for dynamic per-tab PAC routing.
function update_chrome_proxy_config() {
    const pac_script = `
        function FindProxyForURL(url, host) {
            // Evaluated globally per connection request.
            return "DIRECT";
        }
    `;
    
    // Configures Chrome's proxy settings.
    chrome.proxy.settings.set({
        value: {
            mode: "pac_script",
            pacScript: { data: pac_script }
        },
        scope: "regular"
    });
}

// Listen to messages posted by the client if we want to set up a proxy.
chrome.runtime.onMessage.addListener((message, sender, send_response) => {
    if (message.action == "setup_proxy" && sender.tab) {
        
        // Ensure state is loaded before modifying.
        state_loaded.then(async () => {
            const tab_id = sender.tab.id;
            const proxy_string = message.proxy_details;
            
            try {
                const url = new URL(proxy_string);
                let proxy_type = url.protocol.replace(":", "").toLowerCase();
                const host_name = url.hostname;
                const port_num = parseInt(url.port, 10);

                if (["http", "https", "socks5", "socks4"].includes(proxy_type) && host_name && port_num) {
                    const proxy_config = {
                        type: proxy_type,
                        host: host_name,
                        port: port_num
                    };

                    const tab_origin = new URL(sender.tab.url).origin;
                    tab_proxies[tab_id] = proxy_config;
                    origin_proxies[tab_origin] = proxy_config;

                    // Optional proxy auth, parsed as a "protocol://user:pass@host:port" proxy URL.
                    // If no credentials were passed, clear any previously stored ones for this tab/origin.
                    if (url.username) {
                        const credentials = {
                            username: decodeURIComponent(url.username),
                            password: decodeURIComponent(url.password)
                        };
                        tab_proxy_credentials[tab_id] = credentials;
                        origin_proxy_credentials[tab_origin] = credentials;

                        // Attach CDP session to manage authentication challenges.
                        await attach_debugger_if_needed(tab_id);
                    } else {
                        delete tab_proxy_credentials[tab_id];
                        delete origin_proxy_credentials[tab_origin];
                    }

                    save_state();
                    send_response({ success: true });
                } else {
                    send_response({ success: false });
                }
            } catch (err) {
                console.error("[CDP Bridge] Error parsing proxy URL:", err);
                send_response({ success: false });
            }
        });
        return true;
    }
    return false;
});

// Clean up state and detach debugger on tab close.
chrome.tabs.onRemoved.addListener(async (tab_id) => {
    
    // Ensure the state is loaded before modifying.
    await state_loaded;

    if (attached_tabs.has(tab_id)) {
        try {
            await chrome.debugger.detach({ tabId: tab_id });
        } catch (e) {}
        attached_tabs.delete(tab_id);
    }

    let state_changed = false;
    if (tab_proxies[tab_id]) {
        console.log(`[Proxy Bridge] Tab ${tab_id} closed. Clearing proxy mapping.`);
        delete tab_proxies[tab_id];
        state_changed = true;
    }
    if (tab_proxy_credentials[tab_id]) {
        delete tab_proxy_credentials[tab_id];
        state_changed = true;
    }

    if (state_changed) save_state();
});