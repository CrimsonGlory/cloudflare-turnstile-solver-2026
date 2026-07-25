window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    if (event.data.type == "SET_TAB_PROXY") {
        
        let setup_promise = Promise.resolve();

        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_api_spoof_fields) {
            setup_promise = new Promise((resolve) => {
                let script = document.createElement("script");
                
                // Wait for the injected script to signal that it has finished its execution,
                // delete the script and resolve once such occurs.
                document.addEventListener("JS_SPOOF_COMPLETE", function on_complete() {
                    document.removeEventListener("JS_SPOOF_COMPLETE", on_complete);
                    script.remove(); 
                    resolve();
                });

                script.textContent = `
                    (function() {
                        let fields = ${JSON.stringify(event.data.js_api_spoof_fields)};
                        
                        // Create an isolated data store hidden inside this specific closure to prevent global Symbol leaking.
                        if (!document.__spoof_manager_initialized) {
                            Object.defineProperty(document, '__spoof_manager_initialized', { 
                                value: true, 
                                enumerable: false 
                            });
                            
                            let data_store = {};
                            
                            document.addEventListener("__UPDATE_SPOOFS", (e) => {
                                let new_fields = e.detail;
                                for (let key in new_fields) {
                                    data_store[key] = new_fields[key];
                                    
                                    let parts = key.split(".");
                                    let prop = parts.pop();
                                    let obj = window;
                                    
                                    for (let i = 0; i < parts.length; i++) {
                                        if (!obj[parts[i]]) obj[parts[i]] = {}; 
                                        obj = obj[parts[i]];
                                    }
                                    
                                    // Locate the true prototype object, this ensures we don't apply our modifications
                                    // to the wrong instance.
                                    let proto = obj;
                                    let descriptor = null;
                                    while (proto && !descriptor) {
                                        descriptor = Object.getOwnPropertyDescriptor(proto, prop);
                                        if (!descriptor) proto = Object.getPrototypeOf(proto);
                                    }
                                    let target_obj = proto || obj;
                                    
                                    // Check if we have already locked this property on a previous proxy setup.
                                    if (!descriptor || !descriptor.get || !descriptor.get.___mocked) {
                                        let mock_get = function() { return data_store[key]; };
                                        let mock_set = function() {};
                                        
                                        // Spoof the toString methods to perfectly mimic the native function strings that are returned
                                        // by the JS when converting a native C++ function to its string representation.
                                        let native_get_to_string = function() { return "function get " + prop + "() { [native code] }"; };
                                        let native_set_to_string = function() { return "function set " + prop + "() { [native code] }"; };
                                        let native_to_string_to_string = function() { return "function toString() { [native code] }"; };
                                        
                                        // We set our string spoofs so it looks just like what untampered JS would to Cloudflare.
                                        mock_get.toString = native_get_to_string;
                                        mock_set.toString = native_set_to_string;
                                        mock_get.toString.toString = native_to_string_to_string;
                                        mock_set.toString.toString = native_to_string_to_string;
                                        
                                        mock_get.___mocked = true;
                                        
                                        Object.defineProperty(target_obj, prop, {
                                            get: mock_get,
                                            set: mock_set,
                                            configurable: true, 
                                            enumerable: descriptor ? descriptor.enumerable : true
                                        });
                                    }
                                }
                            });
                        }
                        
                        // Parse strings into primitive types if applicable.
                        for (let key in fields) {
                            let val = fields[key];
                            if (val === "true") fields[key] = true;
                            else if (val === "false") fields[key] = false;
                            else if (typeof val == "string" && val.trim() != "" && !isNaN(Number(val))) fields[key] = Number(val);
                        }
                        
                        // Send the payload into our scoped closure.
                        document.dispatchEvent(new CustomEvent("__UPDATE_SPOOFS", { detail: fields }));
                        
                        // Signal back to the extension content script that this script execution is successfully completed.
                        document.dispatchEvent(new CustomEvent("JS_SPOOF_COMPLETE"));
                    })();
                `;
                
                document.documentElement.appendChild(script);
            });
        }

        // Wait for the JS injection to finish first before we proceed.
        setup_promise.then(() => {
            // Forward the requested proxy to the background.
            browser.runtime.sendMessage({
                action: "setup_proxy",
                proxy_details: event.data.proxy_details
            }).then((response) => {
                if (response && response.success) {
                    // Return message to the page telling the client the proxy is ready--async unlocks once received,
                    // giving us a mock lock/await.
                    window.postMessage({ type: "PROXY_READY" }, "*");
                }
            }).catch((err) => {
                console.error("Extension bridge error:", err);
            });
        });
    }
});