// Native toString spoof helper.
let native_to_string = function toString() {
    return "function toString() { [native code] }"; 
};

window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return; 

    if (event.data.type == "SET_TAB_PROXY") { 
        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_API_spoof_fields) { 
            let fields = event.data.js_API_spoof_fields; 
            
            // Create an isolated data store hidden inside this specific closure to prevent global Symbol leaking.
            let data_store = {}; 

            // Parse strings into primitive types if applicable.
            for (let key in fields) { 
                let val = fields[key]; 
                if (val == "true") fields[key] = true; 
                else if (val == "false") fields[key] = false; 
                else if (typeof val === "string" && val.trim() != "" && !isNaN(Number(val))) fields[key] = Number(val); 
            } 

            for (let key in fields) { 
                data_store[key] = fields[key]; 

                let parts = key.split("."); 
                let prop = parts.pop(); 
                let obj = window; 

                for (let i = 0; i < parts.length; i++) { 
                    if (!obj[parts[i]]) { 
                        obj[parts[i]] = {}; 
                    } 
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
                    let mock_get = function () { return data_store[key]; }; 
                    let mock_set = function () {}; 

                    // Spoof the toString methods to perfectly mimic the native function strings that are returned
                    // by the JS when converting a native C++ function to its string representation.
                    let native_get_to_string = function () { return "function get " + prop + "() { [native code] }"; }; 
                    let native_set_to_string = function () { return "function set " + prop + "() { [native code] }"; }; 

                    // We set our string spoofs so it looks just like what untampered JS would to Cloudflare.
                    Object.defineProperty(mock_get, 'toString', { value: native_get_to_string, configurable: true, writable: true }); 
                    Object.defineProperty(mock_set, 'toString', { value: native_set_to_string, configurable: true, writable: true }); 
                    Object.defineProperty(mock_get.toString, 'toString', { value: native_to_string, configurable: true, writable: true }); 
                    Object.defineProperty(mock_set.toString, 'toString', { value: native_to_string, configurable: true, writable: true }); 

                    mock_get.___mocked = true; 

                    Object.defineProperty(target_obj, prop, { 
                        get: mock_get, 
                        set: mock_set, 
                        configurable: true, 
                        enumerable: descriptor ? descriptor.enumerable : true 
                    }); 
                } 
            }

            // matchMedia protection implementation. This only triggers if we have set spoofed window inner dimensions.
            if (data_store['window.innerWidth'] !== undefined || data_store['window.innerHeight'] !== undefined) {
                let orig_matchMedia = window.matchMedia;
                
                if (!orig_matchMedia.___mocked) {
                    let mock_matchMedia = function matchMedia(query) {
                        let mql = orig_matchMedia.call(this, query);
                        
                        let w = data_store['window.innerWidth'];
                        let h = data_store['window.innerHeight'];
                        
                        let is_match = true;
                        let has_dimension_check = false;
                        
                        // Parse standard pixel dimension queries.
                        let w_match = query.match(/(min-width|max-width|width)\s*:\s*(\d+)px/);
                        if (w_match) {
                            has_dimension_check = true;
                            let type = w_match[1];
                            let val = parseInt(w_match[2], 10);
                            if (type == 'width' && w !== val) is_match = false;
                            if (type == 'min-width' && w < val) is_match = false;
                            if (type == 'max-width' && w > val) is_match = false;
                        }
                        
                        let h_match = query.match(/(min-height|max-height|height)\s*:\s*(\d+)px/);
                        if (h_match) {
                            has_dimension_check = true;
                            let type = h_match[1];
                            let val = parseInt(h_match[2], 10);
                            if (type == 'height' && h !== val) is_match = false;
                            if (type == 'min-height' && h < val) is_match = false;
                            if (type == 'max-height' && h > val) is_match = false;
                        }
                        
                        // Only override the match result IF it is a dimension check.
                        if (has_dimension_check) {
                            Object.defineProperty(mql, 'matches', {
                                get: function() { return is_match; },
                                configurable: true,
                                enumerable: true
                            });
                        }
                        
                        return mql;
                    };
                    
                    // Spoof toStrings here so it looks like actual native, untampered JS.
                    let native_matchMedia_to_string = function () { return "function matchMedia() { [native code] }"; };
                    Object.defineProperty(mock_matchMedia, 'toString', { value: native_matchMedia_to_string, configurable: true, writable: true });
                    Object.defineProperty(mock_matchMedia.toString, 'toString', { value: native_to_string, configurable: true, writable: true });
                    
                    mock_matchMedia.___mocked = true;
                    
                    Object.defineProperty(window, 'matchMedia', {
                        value: mock_matchMedia,
                        configurable: true,
                        writable: true
                    });
                }
            }
        }

        // Forward the requested proxy to the isolated script bridge.
        window.postMessage({ 
            type: "FORWARD_SETUP_PROXY", 
            proxy_details: event.data.proxy_details 
        }, "*"); 
    }
});