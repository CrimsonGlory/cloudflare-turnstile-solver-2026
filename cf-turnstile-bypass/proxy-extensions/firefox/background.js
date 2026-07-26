// Object map for proxy ID info.
let tab_proxies = {};
let origin_proxies = {};

// Object map for per-tab/per-origin proxy authentication credentials.
let tab_proxy_credentials = {};
let origin_proxy_credentials = {};

// Load state from local storage on script wakeup to prevent data loss after idling.
let state_loaded = browser.storage.local.get([
    'tab_proxies', 'origin_proxies', 'tab_proxy_credentials', 'origin_proxy_credentials'
]).then((res) => {
    tab_proxies = res.tab_proxies || {};
    origin_proxies = res.origin_proxies || {};
    tab_proxy_credentials = res.tab_proxy_credentials || {};
    origin_proxy_credentials = res.origin_proxy_credentials || {};
});

// Firefox Manifest V3 background scripts suspend when idle. This saves our state before the script sleeps.
function save_state() {
    browser.storage.local.set({
        tab_proxies,
        origin_proxies,
        tab_proxy_credentials,
        origin_proxy_credentials
    });
}

// Prevent WebRTC from leaking the host's local IP address (peeking), 
// without disabling WebRTC itself.
if (browser.privacy && browser.privacy.network && browser.privacy.network.webRTCIPHandlingPolicy) {
    browser.privacy.network.webRTCIPHandlingPolicy.set({ value: "default_public_interface_only" });
}

// Handle proxy routing dynamically via Firefox's native API.
browser.proxy.onRequest.addListener(
    async (requestDetails) => {
        // Ensure state has resolved before attempting to pull proxy data on wakeup.
        await state_loaded; 
        
        let tab_id = requestDetails.tabId;
        
        if (tab_proxies[tab_id]) {
            return tab_proxies[tab_id];
        }

        try {
            let request_origin = new URL(requestDetails.url).origin;
            if (origin_proxies[request_origin]) {
                return origin_proxies[request_origin];
            }
        } catch(e) {}
        
        return { type: "direct" };
    },
    { urls: ["<all_urls>"] }
);

// Answer proxy authentication challenges natively with stored credentials.
browser.webRequest.onAuthRequired.addListener(
    async (details) => {
        // Ensure state has resolved before attempting to pull auth data on wakeup.
        await state_loaded; 

        if (!details.isProxy) return;

        let tab_id = details.tabId;
        let creds = tab_proxy_credentials[tab_id];

        if (!creds && details.url) {
            try {
                let request_origin = new URL(details.url).origin;
                creds = origin_proxy_credentials[request_origin];
            } catch (e) {}
        }

        if (creds) {
            return {
                authCredentials: {
                    username: creds.username,
                    password: creds.password
                }
            };
        }
        
        return { cancel: false }; 
    },
    { urls: ["<all_urls>"] },
    ["blocking"]
);

// Listen to messages posted by the client if we want to set up a proxy.
browser.runtime.onMessage.addListener((message, sender) => {
    if (message.action == "setup_proxy" && sender.tab) {
        
        // Return the Promise directly to respond asynchronously in MV3
        return state_loaded.then(async () => {
            let tab_id = sender.tab.id;
            let proxy_string = message.proxy_details;
            
            try {
                let url = new URL(proxy_string);
                let proxy_type = url.protocol.replace(":", "").toLowerCase();
                let host_name = url.hostname;
                let port_num = parseInt(url.port, 10);

                if (["http", "https", "socks5", "socks4", "socks"].includes(proxy_type) && host_name && port_num) {
                    
                    // Firefox's proxy API expects 'socks' instead of 'socks5'.
                    if (proxy_type === "socks5") proxy_type = "socks";
                    
                    let proxy_config = {
                        type: proxy_type,
                        host: host_name,
                        port: port_num
                    };

                    let tab_origin = new URL(sender.tab.url).origin;
                    
                    // Firefox's proxy API requires objects inside an array.
                    tab_proxies[tab_id] = [proxy_config];
                    origin_proxies[tab_origin] = [proxy_config];

                    // Optional proxy auth, parsed as a "protocol://user:pass@host:port" proxy URL.
                    if (url.username) {
                        let credentials = {
                            username: decodeURIComponent(url.username),
                            password: decodeURIComponent(url.password)
                        };
                        tab_proxy_credentials[tab_id] = credentials;
                        origin_proxy_credentials[tab_origin] = credentials;
                    } else {
                        delete tab_proxy_credentials[tab_id];
                        delete origin_proxy_credentials[tab_origin];
                    }

                    save_state();
                    return { success: true };
                } else {
                    return { success: false };
                }
            } catch (err) {
                console.error("[Proxy Bridge] Error parsing proxy URL:", err);
                return { success: false };
            }
        });
    }
    return false;
});

// Clean up state and detach proxy mapping on tab close.
browser.tabs.onRemoved.addListener(async (tab_id) => {
    await state_loaded;

    let state_changed = false;
    if (tab_proxies[tab_id]) {
        delete tab_proxies[tab_id];
        state_changed = true;
    }
    if (tab_proxy_credentials[tab_id]) {
        delete tab_proxy_credentials[tab_id];
        state_changed = true;
    }

    if (state_changed) save_state();
});