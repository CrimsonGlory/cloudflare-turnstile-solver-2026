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

// FireFox scripts can idle. This will save our state before the script idles.
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

// Listen to messages posted by the client if we want to set up a proxy.
browser.runtime.onMessage.addListener((message, sender, send_response) => {
    if (message.action == "setup_proxy" && sender.tab) {
        // Ensure state is loaded before modifying.
        state_loaded.then(() => {
            let tab_id = sender.tab.id;
            let proxy_string = message.proxy_details;
            let tab_origin = new URL(sender.tab.url).origin;
            
            try {
                let url = new URL(proxy_string);
                let proxy_type = url.protocol.replace(":", "").toLowerCase();
                
                // Firefox requires the extension be written as "socks" for socks5 proxies.
                // This does not apply to socks4.
                if (proxy_type == "socks5") proxy_type = "socks";

                let host_name = url.hostname;
                let port_num = parseInt(url.port, 10);

                if (["http", "https", "socks", "socks4"].includes(proxy_type) && host_name && port_num) {
                    
                    let proxy_config = { 
                        type: proxy_type, 
                        host: host_name, 
                        port: port_num,
                        proxyDNS: true 
                    };

                    tab_proxies[tab_id] = proxy_config;
                    origin_proxies[tab_origin] = proxy_config;

                    // Optional proxy auth, parsed as a "protocol://user:pass@host:port" proxy URL.
                    // If no credentials were passed, clear any previously stored ones for this tab/origin.
                    if (url.username) {
                        let proxy_credentials = {
                            username: decodeURIComponent(url.username),
                            password: decodeURIComponent(url.password)
                        };
                        tab_proxy_credentials[tab_id] = proxy_credentials;
                        origin_proxy_credentials[tab_origin] = proxy_credentials;
                        console.log(`[Proxy Bridge] Tab ${tab_id} proxy auth credentials stored for ${host_name}:${port_num}`);
                    } else {
                        delete tab_proxy_credentials[tab_id];
                        delete origin_proxy_credentials[tab_origin];
                    }
                    
                    save_state();
                    console.log(`[Proxy Bridge] Tab ${tab_id} bound to ${proxy_type}://${host_name}:${port_num}`);
                    send_response({ success: true });
                } else {
                    console.error(`[Proxy Bridge] Invalid proxy format or unsupported protocol: ${proxy_type}`);
                    send_response({ success: false });
                }
            } catch (err) {
                console.error("[Proxy Bridge] Failed to parse proxy URL.", err);
                send_response({ success: false });
            }
        });
        return true;
    }
    return false; 
});

// Listen and tunnel requests if a proxy is set.
browser.proxy.onRequest.addListener(
    async (details) => {
        // Wait for state to load from storage if the script is waking up.
        await state_loaded; 

        let proxy_info = tab_proxies[details.tabId];
        
        if (!proxy_info && details.tabId == -1) {
            let request_origin = details.originUrl ? new URL(details.originUrl).origin : null;
            if (request_origin && origin_proxies[request_origin]) {
                proxy_info = origin_proxies[request_origin];
            }
        }
        
        if (proxy_info) {
            return [proxy_info]; 
        }
        
        return [{ type: "direct" }]; 
    },
    { urls: ["<all_urls>"] }
);

browser.proxy.onError.addListener(error => {
    console.error(`[Proxy Bridge] Network error:`, error.message);
});

// Answer proxy authentication challenges with any stored credentials.
// Firefox has a separate onAuthRequired listener for this, which we use here.
browser.webRequest.onAuthRequired.addListener(
    async (details) => {
        if (!details.isProxy) return {};
        
        // Wait for state to load from storage if the script is waking up.
        await state_loaded; 

        let proxy_credentials = tab_proxy_credentials[details.tabId];

        if (!proxy_credentials && details.tabId == -1) {
            let request_origin = details.originUrl ? new URL(details.originUrl).origin : null;
            if (request_origin && origin_proxy_credentials[request_origin]) {
                proxy_credentials = origin_proxy_credentials[request_origin];
            }
        }

        if (proxy_credentials) {
            return { authCredentials: proxy_credentials };
        }

        return {};
    },
    { urls: ["<all_urls>"] },
    ["blocking"]
);

browser.tabs.onRemoved.addListener(async (tab_id) => {
    // Ensure the state is loaded before modifying.
    await state_loaded; 

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