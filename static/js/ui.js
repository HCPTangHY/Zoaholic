/**
 * Material Design 3 UI Component Library - Enhanced Version
 * Provides reusable MD3-compliant components with full feature set
 */
const UI = {
    // ==================== Utility Functions ====================
    
    /**
     * Create a generic element with classes and text
     */
    el: (tag, classes = "", text = "") => {
        const element = document.createElement(tag);
        if (classes) element.className = classes;
        if (text) element.textContent = text;
        return element;
    },

    /**
     * Generate unique ID
     */
    uniqueId: (prefix = "md") => {
        return `${prefix}-${Math.random().toString(36).substr(2, 9)}`;
    },

    /**
     * Material Symbols Icon (MD3 Style)
     */
    icon: (name, classes = "", filled = false) => {
        const i = document.createElement("span");
        i.className = `material-symbols-outlined ${classes}`;
        if (filled) {
            i.style.fontVariationSettings = "'FILL' 1, 'wght' 400, 'GRAD' 0, 'opsz' 24";
        }
        i.textContent = name;
        return i;
    },

    // ==================== Card Component ====================
    
    /**
     * MD3 Card Component
     */
    card: (variant = "filled", classes = "") => {
        const variantClasses = {
            elevated: "bg-md-surface-container-low md-elevation-1",
            filled: "bg-md-surface-container-highest",
            outlined: "bg-md-surface border border-md-outline"
        };
        
        const div = document.createElement("div");
        div.className = `rounded-md-lg p-6 ${variantClasses[variant] || variantClasses.filled} ${classes}`;
        return div;
    },

    // ==================== Button Component ====================
    
    /**
     * MD3 Button Component with loading state
     */
    btn: (text, onClick, variant = "filled", iconName = null, options = {}) => {
        const btn = document.createElement("button");
        const { loading = false, disabled = false } = options;
        
        const variantClasses = {
            filled: "bg-md-primary text-md-on-primary hover:shadow-md-1",
            outlined: "bg-transparent text-md-primary border border-md-outline hover:bg-md-primary/8",
            text: "bg-transparent text-md-primary hover:bg-md-primary/8",
            elevated: "bg-md-surface-container-low text-md-primary md-elevation-1 hover:md-elevation-2",
            tonal: "bg-md-secondary-container text-md-on-secondary-container hover:shadow-md-1"
        };
        
        const baseClass = "inline-flex items-center justify-center gap-2 px-6 py-2.5 rounded-md-full font-medium text-label-large transition-all duration-200 md-state-layer disabled:opacity-38 disabled:cursor-not-allowed disabled:pointer-events-none";
        
        btn.className = `${baseClass} ${variantClasses[variant] || variantClasses.filled}`;
        btn.disabled = disabled || loading;
        
        const renderContent = (isLoading) => {
            btn.innerHTML = "";
            
            if (isLoading) {
                const spinner = UI.progressCircular(20, true);
                spinner.classList.add("mr-1");
                btn.appendChild(spinner);
                const span = document.createElement("span");
                span.textContent = "加载�?..";
                btn.appendChild(span);
            } else {
                if (iconName) {
                    btn.appendChild(UI.icon(iconName, "text-lg"));
                }
                const span = document.createElement("span");
                span.textContent = text;
                btn.appendChild(span);
            }
        };
        
        renderContent(loading);
        
        btn.setLoading = (isLoading) => {
            btn.disabled = isLoading;
            renderContent(isLoading);
        };
        
        if (onClick && !loading) {
            btn.onclick = onClick;
        }
        
        return btn;
    },

    /**
     * MD3 Icon Button
     */
    iconBtn: (iconName, onClick, variant = "standard", options = {}) => {
        const btn = document.createElement("button");
        const { disabled = false, tooltip = null, filled = false } = options;
        
        const variantClasses = {
            standard: "text-md-on-surface-variant hover:bg-md-on-surface/8",
            filled: "bg-md-primary text-md-on-primary hover:shadow-md-1",
            tonal: "bg-md-secondary-container text-md-on-secondary-container hover:shadow-md-1",
            outlined: "border border-md-outline text-md-on-surface-variant hover:bg-md-on-surface/8"
        };
        
        btn.className = `w-10 h-10 rounded-full flex items-center justify-center md-state-layer transition-all ${variantClasses[variant]} disabled:opacity-38 disabled:cursor-not-allowed`;
        btn.disabled = disabled;
        btn.appendChild(UI.icon(iconName, "", filled));
        
        if (tooltip) {
            btn.setAttribute("data-tooltip", tooltip);
        }
        
        if (onClick) btn.onclick = onClick;
        return btn;
    },

    /**
     * MD3 FAB (Floating Action Button)
     */
    fab: (iconName, onClick, variant = "primary", size = "medium", options = {}) => {
        const btn = document.createElement("button");
        const { label = null, disabled = false } = options;
        
        const variantClasses = {
            primary: "bg-md-primary-container text-md-on-primary-container",
            secondary: "bg-md-secondary-container text-md-on-secondary-container",
            tertiary: "bg-md-tertiary-container text-md-on-tertiary-container",
            surface: "bg-md-surface-container-high text-md-primary"
        };
        
        const sizeClasses = {
            small: "w-10 h-10",
            medium: "w-14 h-14",
            large: "w-24 h-24"
        };
        
        if (label) {
            btn.className = `h-14 px-4 rounded-md-lg md-elevation-3 hover:md-elevation-4 flex items-center gap-3 transition-all duration-200 md-state-layer ${variantClasses[variant]} disabled:opacity-38`;
            btn.appendChild(UI.icon(iconName, "text-2xl"));
            const labelSpan = UI.el("span", "text-label-large font-medium", label);
            btn.appendChild(labelSpan);
        } else {
            btn.className = `${sizeClasses[size]} rounded-md-lg md-elevation-3 hover:md-elevation-4 flex items-center justify-center transition-all duration-200 md-state-layer ${variantClasses[variant]} disabled:opacity-38`;
            btn.appendChild(UI.icon(iconName, size === "large" ? "text-4xl" : "text-2xl"));
        }
        
        btn.disabled = disabled;
        if (onClick) btn.onclick = onClick;
        return btn;
    },

    // ==================== Text Field Component ====================
    
    /**
     * MD3 Text Field (Outlined & Filled variants)
     */
    textField: (label, placeholder = "", type = "text", value = "", options = {}) => {
        const {
            variant = "outlined",
            error = false,
            helperText = "",
            disabled = false,
            required = false,
            leadingIcon = null,
            trailingIcon = null
        } = options;
        
        const wrapper = document.createElement("div");
        wrapper.className = "relative mb-4";
        
        const inputContainer = document.createElement("div");
        inputContainer.className = "relative flex items-center";
        
        const input = document.createElement("input");
        input.type = type;
        input.disabled = disabled;
        input.required = required;
        input.placeholder = " ";
        if (value !== undefined && value !== null && value !== "") {
            input.value = value;
        }
        
        const baseInputClass = "peer w-full text-body-large text-md-on-surface focus:outline-none transition-all disabled:opacity-38 disabled:cursor-not-allowed";
        
        const outlinedClass = `${baseInputClass} px-4 pt-6 pb-2 bg-transparent border rounded-md-xs ${
            error 
                ? "border-md-error focus:border-md-error focus:border-2" 
                : "border-md-outline focus:border-md-primary focus:border-2"
        }`;
        
        const filledClass = `${baseInputClass} px-4 pt-6 pb-2 bg-md-surface-container-highest rounded-t-md-xs border-b-2 ${
            error 
                ? "border-md-error focus:border-md-error" 
                : "border-md-on-surface-variant focus:border-md-primary"
        }`;
        
        input.className = variant === "filled" ? filledClass : outlinedClass;
        
        if (leadingIcon) {
            input.classList.add("pl-12");
        }
        if (trailingIcon) {
            input.classList.add("pr-12");
        }
        
        const labelEl = document.createElement("label");
        const hasValue = value !== undefined && value !== null && value !== "";
        const baseLabelClass = `absolute font-medium pointer-events-none transition-all duration-200 ${
            error ? "text-md-error" : "text-md-on-surface-variant peer-focus:text-md-primary"
        }`;
        
        labelEl.className = hasValue
            ? `${baseLabelClass} left-4 top-2 text-body-small`
            : `${baseLabelClass} left-4 top-1/2 -translate-y-1/2 text-body-large`;
        
        if (leadingIcon && !hasValue) {
            labelEl.classList.remove("left-4");
            labelEl.classList.add("left-12");
        }
        
        labelEl.textContent = label + (required ? " *" : "");
        
        if (leadingIcon) {
            const iconEl = UI.icon(leadingIcon, `absolute left-3 top-1/2 -translate-y-1/2 ${error ? "text-md-error" : "text-md-on-surface-variant"}`);
            inputContainer.appendChild(iconEl);
        }
        
        inputContainer.appendChild(input);
        inputContainer.appendChild(labelEl);
        
        if (trailingIcon) {
            const iconEl = UI.icon(trailingIcon, `absolute right-3 top-1/2 -translate-y-1/2 ${error ? "text-md-error" : "text-md-on-surface-variant"}`);
            inputContainer.appendChild(iconEl);
        }
        
        wrapper.appendChild(inputContainer);
        
        if (helperText) {
            const helperEl = UI.el("div", `text-body-small mt-1 px-4 ${error ? "text-md-error" : "text-md-on-surface-variant"}`, helperText);
            wrapper.appendChild(helperEl);
        }
        
        input.addEventListener("focus", () => {
            labelEl.className = `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${
                error ? "text-md-error" : "text-md-primary"
            }`;
            if (!input.value) {
                input.placeholder = placeholder;
            }
        });
        
        input.addEventListener("blur", () => {
            input.placeholder = " ";
            const hasVal = input.value && input.value.trim() !== "";
            labelEl.className = hasVal
                ? `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`
                : `absolute left-4 top-1/2 -translate-y-1/2 text-body-large font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`;
            
            if (leadingIcon && !hasVal) {
                labelEl.classList.remove("left-4");
                labelEl.classList.add("left-12");
            }
        });
        
        wrapper.setError = (isError, message = "") => {
            if (isError) {
                input.classList.remove("border-md-outline", "focus:border-md-primary");
                input.classList.add("border-md-error", "focus:border-md-error");
            } else {
                input.classList.remove("border-md-error", "focus:border-md-error");
                input.classList.add("border-md-outline", "focus:border-md-primary");
            }
            
            const existingHelper = wrapper.querySelector(".text-body-small");
            if (existingHelper) {
                existingHelper.textContent = message || helperText;
                existingHelper.className = `text-body-small mt-1 px-4 ${isError ? "text-md-error" : "text-md-on-surface-variant"}`;
            } else if (message) {
                const helperEl = UI.el("div", `text-body-small mt-1 px-4 ${isError ? "text-md-error" : "text-md-on-surface-variant"}`, message);
                wrapper.appendChild(helperEl);
            }
        };
        
        return { wrapper, input };
    },

    /**
     * MD3 Text Area
     */
    textArea: (label, placeholder = "", value = "", rows = 3, options = {}) => {
        const {
            variant = "outlined",
            error = false,
            helperText = "",
            disabled = false,
            required = false
        } = options;
        
        const wrapper = document.createElement("div");
        wrapper.className = "relative mb-4";
        
        const textarea = document.createElement("textarea");
        textarea.disabled = disabled;
        textarea.required = required;
        textarea.placeholder = " ";
        textarea.rows = rows;
        if (value !== undefined && value !== null && value !== "") {
            textarea.value = value;
        }
        
        const baseClass = "peer w-full text-body-large text-md-on-surface focus:outline-none transition-all resize-y font-mono disabled:opacity-38 disabled:cursor-not-allowed";
        
        const outlinedClass = `${baseClass} px-4 pt-6 pb-2 bg-transparent border rounded-md-xs ${
            error 
                ? "border-md-error focus:border-md-error focus:border-2" 
                : "border-md-outline focus:border-md-primary focus:border-2"
        }`;
        
        const filledClass = `${baseClass} px-4 pt-6 pb-2 bg-md-surface-container-highest rounded-t-md-xs border-b-2 ${
            error 
                ? "border-md-error focus:border-md-error" 
                : "border-md-on-surface-variant focus:border-md-primary"
        }`;
        
        textarea.className = variant === "filled" ? filledClass : outlinedClass;
        
        const labelEl = document.createElement("label");
        const hasValue = value !== undefined && value !== null && value !== "";
        labelEl.className = hasValue
            ? `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`
            : `absolute left-4 top-6 text-body-large font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`;
        labelEl.textContent = label + (required ? " *" : "");
        
        textarea.addEventListener("focus", () => {
            labelEl.className = `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-primary"}`;
            if (!textarea.value) {
                textarea.placeholder = placeholder;
            }
        });
        
        textarea.addEventListener("blur", () => {
            textarea.placeholder = " ";
            const hasVal = textarea.value && textarea.value.trim() !== "";
            labelEl.className = hasVal
                ? `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`
                : `absolute left-4 top-6 text-body-large font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant"}`;
        });
        
        wrapper.appendChild(textarea);
        wrapper.appendChild(labelEl);
        
        if (helperText) {
            const helperEl = UI.el("div", `text-body-small mt-1 px-4 ${error ? "text-md-error" : "text-md-on-surface-variant"}`, helperText);
            wrapper.appendChild(helperEl);
        }
        
        return { wrapper, input: textarea };
    },

    // ==================== Selection Controls ====================
    
    /**
     * MD3 Checkbox Component
     */
    checkbox: (label, checked = false, onChange = null, options = {}) => {
        const { disabled = false, indeterminate = false } = options;
        
        const wrapper = document.createElement("label");
        wrapper.className = `inline-flex items-center gap-3 cursor-pointer ${disabled ? "opacity-38 cursor-not-allowed" : ""}`;
        
        const checkboxContainer = document.createElement("div");
        checkboxContainer.className = "relative w-5 h-5";
        
        const input = document.createElement("input");
        input.type = "checkbox";
        input.className = "sr-only peer";
        input.checked = checked;
        input.disabled = disabled;
        input.indeterminate = indeterminate;
        
        const box = document.createElement("div");
        box.className = `w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center ${
            checked || indeterminate
                ? "bg-md-primary border-md-primary"
                : "border-md-on-surface-variant hover:border-md-on-surface"
        }`;
        
        const checkIcon = UI.icon(indeterminate ? "remove" : "check", `text-sm text-md-on-primary transition-transform ${checked || indeterminate ? "scale-100" : "scale-0"}`);
        box.appendChild(checkIcon);
        
        input.addEventListener("change", (e) => {
            const isChecked = e.target.checked;
            input.indeterminate = false;
            
            box.className = `w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center ${
                isChecked
                    ? "bg-md-primary border-md-primary"
                    : "border-md-on-surface-variant hover:border-md-on-surface"
            }`;
            
            checkIcon.textContent = "check";
            checkIcon.className = `text-sm text-md-on-primary transition-transform ${isChecked ? "scale-100" : "scale-0"}`;
            
            if (onChange) onChange(isChecked);
        });
        
        checkboxContainer.appendChild(input);
        checkboxContainer.appendChild(box);
        
        const labelText = UI.el("span", "text-body-medium text-md-on-surface", label);
        
        wrapper.appendChild(checkboxContainer);
        wrapper.appendChild(labelText);
        
        wrapper.setChecked = (isChecked) => {
            input.checked = isChecked;
            input.dispatchEvent(new Event("change"));
        };
        
        wrapper.setIndeterminate = (isIndeterminate) => {
            input.indeterminate = isIndeterminate;
            if (isIndeterminate) {
                box.className = "w-5 h-5 rounded-sm border-2 transition-all flex items-center justify-center bg-md-primary border-md-primary";
                checkIcon.textContent = "remove";
                checkIcon.className = "text-sm text-md-on-primary transition-transform scale-100";
            }
        };
        
        return wrapper;
    },

    /**
     * MD3 Radio Button Component
     */
    radio: (label, name, value, checked = false, onChange = null, options = {}) => {
        const { disabled = false } = options;
        
        const wrapper = document.createElement("label");
        wrapper.className = `inline-flex items-center gap-3 cursor-pointer ${disabled ? "opacity-38 cursor-not-allowed" : ""}`;
        
        const radioContainer = document.createElement("div");
        radioContainer.className = "relative w-5 h-5";
        
        const input = document.createElement("input");
        input.type = "radio";
        input.name = name;
        input.value = value;
        input.className = "sr-only peer";
        input.checked = checked;
        input.disabled = disabled;
        
        const circle = document.createElement("div");
        circle.className = `w-5 h-5 rounded-full border-2 transition-all flex items-center justify-center ${
            checked
                ? "border-md-primary"
                : "border-md-on-surface-variant hover:border-md-on-surface"
        }`;
        
        const dot = document.createElement("div");
        dot.className = `w-2.5 h-2.5 rounded-full bg-md-primary transition-transform ${checked ? "scale-100" : "scale-0"}`;
        circle.appendChild(dot);
        
        input.addEventListener("change", (e) => {
            document.querySelectorAll(`input[name="${name}"]`).forEach((radio) => {
                const parentCircle = radio.parentElement.querySelector("div");
                const parentDot = parentCircle?.querySelector("div");
                if (parentCircle && parentDot) {
                    if (radio.checked) {
                        parentCircle.className = "w-5 h-5 rounded-full border-2 transition-all flex items-center justify-center border-md-primary";
                        parentDot.className = "w-2.5 h-2.5 rounded-full bg-md-primary transition-transform scale-100";
                    } else {
                        parentCircle.className = "w-5 h-5 rounded-full border-2 transition-all flex items-center justify-center border-md-on-surface-variant hover:border-md-on-surface";
                        parentDot.className = "w-2.5 h-2.5 rounded-full bg-md-primary transition-transform scale-0";
                    }
                }
            });
            
            if (onChange) onChange(e.target.value);
        });
        
        radioContainer.appendChild(input);
        radioContainer.appendChild(circle);
        
        const labelText = UI.el("span", "text-body-medium text-md-on-surface", label);
        
        wrapper.appendChild(radioContainer);
        wrapper.appendChild(labelText);
        
        return wrapper;
    },

    /**
     * MD3 Switch Component (Fixed)
     */
    switch: (label, checked = false, onChange = null, options = {}) => {
        const { disabled = false } = options;
        
        const wrapper = document.createElement("label");
        wrapper.className = `inline-flex items-center gap-3 cursor-pointer ${disabled ? "opacity-38 cursor-not-allowed" : ""}`;
        
        const switchContainer = document.createElement("div");
        switchContainer.className = "relative inline-block";
        
        const input = document.createElement("input");
        input.type = "checkbox";
        input.className = "sr-only";
        input.checked = checked;
        input.disabled = disabled;
        
        const track = document.createElement("div");
        track.className = `w-[52px] h-8 rounded-full flex items-center transition-colors duration-200 ${
            checked
                ? "bg-md-primary justify-end pr-1"
                : "bg-md-surface-variant border-2 border-md-outline justify-start pl-1"
        }`;
        
        const thumb = document.createElement("div");
        thumb.className = `rounded-full shadow-md flex items-center justify-center transition-all duration-200 ${
            checked
                ? "w-6 h-6 bg-md-on-primary"
                : "w-4 h-4 bg-md-outline"
        }`;
        
        if (checked) {
            const checkIcon = UI.icon("check", "text-xs text-md-primary");
            thumb.appendChild(checkIcon);
        }
        
        input.addEventListener("change", (e) => {
            const isChecked = e.target.checked;
            
            track.className = `w-[52px] h-8 rounded-full flex items-center transition-colors duration-200 ${
                isChecked
                    ? "bg-md-primary justify-end pr-1"
                    : "bg-md-surface-variant border-2 border-md-outline justify-start pl-1"
            }`;
            
            thumb.className = `rounded-full shadow-md flex items-center justify-center transition-all duration-200 ${
                isChecked
                    ? "w-6 h-6 bg-md-on-primary"
                    : "w-4 h-4 bg-md-outline"
            }`;
            
            thumb.innerHTML = "";
            if (isChecked) {
                thumb.appendChild(UI.icon("check", "text-xs text-md-primary"));
            }
            
            if (onChange) onChange(isChecked);
        });
        
        track.appendChild(thumb);
        switchContainer.appendChild(input);
        switchContainer.appendChild(track);
        
        const labelText = UI.el("span", "text-body-medium text-md-on-surface", label);
        
        wrapper.appendChild(switchContainer);
        wrapper.appendChild(labelText);
        
        wrapper.setChecked = (isChecked) => {
            input.checked = isChecked;
            input.dispatchEvent(new Event("change"));
        };
        
        return wrapper;
    },

    // ==================== Chip Component ====================
    
    /**
     * MD3 Chip Component with selection support
     */
    chip: (label, variant = "assist", iconName = null, options = {}) => {
        const { selected = false, onRemove = null, onClick = null, disabled = false } = options;
        
        const chip = document.createElement("button");
        chip.disabled = disabled;
        
        const getVariantClasses = (isSelected) => {
            const base = {
                assist: isSelected 
                    ? "bg-md-secondary-container border-md-secondary-container text-md-on-secondary-container"
                    : "bg-transparent border-md-outline text-md-on-surface",
                filter: isSelected 
                    ? "bg-md-secondary-container border-md-secondary-container text-md-on-secondary-container"
                    : "bg-transparent border-md-outline text-md-on-surface-variant",
                input: "bg-transparent border-md-outline text-md-on-surface",
                suggestion: isSelected
                    ? "bg-md-secondary-container border-md-secondary-container text-md-on-secondary-container"
                    : "bg-transparent border-md-outline text-md-on-surface-variant"
            };
            return base[variant] || base.assist;
        };
        
        let isSelected = selected;
        chip.className = `inline-flex items-center gap-2 px-4 py-1.5 rounded-md-sm border ${getVariantClasses(isSelected)} hover:shadow-md-1 transition-all md-state-layer text-label-large disabled:opacity-38 disabled:cursor-not-allowed`;
        
        const renderContent = () => {
            chip.innerHTML = "";
            
            if (variant === "filter" && isSelected) {
                chip.appendChild(UI.icon("check", "text-lg"));
            } else if (iconName) {
                chip.appendChild(UI.icon(iconName, "text-lg"));
            }
            
            const span = document.createElement("span");
            span.textContent = label;
            chip.appendChild(span);
            
            if (onRemove && variant === "input") {
                const removeBtn = document.createElement("span");
                removeBtn.className = "w-5 h-5 rounded-full flex items-center justify-center hover:bg-md-on-surface/12 transition-colors cursor-pointer";
                removeBtn.appendChild(UI.icon("close", "text-sm"));
                removeBtn.onclick = (e) => {
                    e.stopPropagation();
                    onRemove();
                };
                chip.appendChild(removeBtn);
            }
        };
        
        renderContent();
        
        if (onClick) {
            chip.onclick = () => {
                if (variant === "filter" || variant === "suggestion") {
                    isSelected = !isSelected;
                    chip.className = `inline-flex items-center gap-2 px-4 py-1.5 rounded-md-sm border ${getVariantClasses(isSelected)} hover:shadow-md-1 transition-all md-state-layer text-label-large disabled:opacity-38 disabled:cursor-not-allowed`;
                    renderContent();
                }
                onClick(isSelected);
            };
        }
        
        chip.setSelected = (sel) => {
            isSelected = sel;
            chip.className = `inline-flex items-center gap-2 px-4 py-1.5 rounded-md-sm border ${getVariantClasses(isSelected)} hover:shadow-md-1 transition-all md-state-layer text-label-large disabled:opacity-38 disabled:cursor-not-allowed`;
            renderContent();
        };
        
        chip.isSelected = () => isSelected;
        
        return chip;
    },

    // ==================== Progress Indicators ====================
    
    /**
     * MD3 Linear Progress Indicator
     */
    progressLinear: (value = null, classes = "") => {
        const wrapper = document.createElement("div");
        wrapper.className = `w-full h-1 bg-md-surface-container-highest rounded-full overflow-hidden ${classes}`;
        
        const track = document.createElement("div");
        
        if (value === null) {
            track.className = "h-full bg-md-primary rounded-full animate-progress-indeterminate";
            track.style.width = "30%";
        } else {
            track.className = "h-full bg-md-primary rounded-full transition-all duration-300";
            track.style.width = `${Math.min(100, Math.max(0, value))}%`;
        }
        
        wrapper.appendChild(track);
        
        wrapper.setValue = (newValue) => {
            if (newValue === null) {
                track.className = "h-full bg-md-primary rounded-full animate-progress-indeterminate";
                track.style.width = "30%";
            } else {
                track.className = "h-full bg-md-primary rounded-full transition-all duration-300";
                track.style.width = `${Math.min(100, Math.max(0, newValue))}%`;
            }
        };
        
        return wrapper;
    },

    /**
     * MD3 Circular Progress Indicator
     */
    progressCircular: (size = 48, indeterminate = true, value = 0) => {
        const wrapper = document.createElement("div");
        wrapper.className = `inline-flex items-center justify-center ${indeterminate ? "animate-spin" : ""}`;
        wrapper.style.width = `${size}px`;
        wrapper.style.height = `${size}px`;
        
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 48 48");
        svg.style.width = "100%";
        svg.style.height = "100%";
        
        const strokeWidth = 4;
        const radius = 20;
        const circumference = 2 * Math.PI * radius;
        
        const bgCircle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        bgCircle.setAttribute("cx", "24");
        bgCircle.setAttribute("cy", "24");
        bgCircle.setAttribute("r", String(radius));
        bgCircle.setAttribute("fill", "none");
        bgCircle.setAttribute("stroke", "currentColor");
        bgCircle.setAttribute("stroke-width", String(strokeWidth));
        bgCircle.setAttribute("class", "text-md-surface-container-highest");
        
        const progressCircle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        progressCircle.setAttribute("cx", "24");
        progressCircle.setAttribute("cy", "24");
        progressCircle.setAttribute("r", String(radius));
        progressCircle.setAttribute("fill", "none");
        progressCircle.setAttribute("stroke", "currentColor");
        progressCircle.setAttribute("stroke-width", String(strokeWidth));
        progressCircle.setAttribute("stroke-linecap", "round");
        progressCircle.setAttribute("class", "text-md-primary");
        progressCircle.style.transformOrigin = "center";
        progressCircle.style.transform = "rotate(-90deg)";
        
        if (indeterminate) {
            progressCircle.setAttribute("stroke-dasharray", `${circumference * 0.25} ${circumference * 0.75}`);
        } else {
            const progress = Math.min(100, Math.max(0, value));
            const dashOffset = circumference * (1 - progress / 100);
            progressCircle.setAttribute("stroke-dasharray", String(circumference));
            progressCircle.setAttribute("stroke-dashoffset", String(dashOffset));
        }
        
        svg.appendChild(bgCircle);
        svg.appendChild(progressCircle);
        wrapper.appendChild(svg);
        
        wrapper.setValue = (newValue) => {
            wrapper.classList.remove("animate-spin");
            const progress = Math.min(100, Math.max(0, newValue));
            const dashOffset = circumference * (1 - progress / 100);
            progressCircle.setAttribute("stroke-dasharray", String(circumference));
            progressCircle.setAttribute("stroke-dashoffset", String(dashOffset));
            progressCircle.style.transition = "stroke-dashoffset 0.3s ease";
        };
        
        wrapper.setIndeterminate = () => {
            wrapper.classList.add("animate-spin");
            progressCircle.setAttribute("stroke-dasharray", `${circumference * 0.25} ${circumference * 0.75}`);
            progressCircle.removeAttribute("stroke-dashoffset");
            progressCircle.style.transition = "";
        };
        
        return wrapper;
    },

    /**
     * MD3 Loading Spinner
     */
    spinner: (size = 48) => {
        const wrapper = document.createElement("div");
        wrapper.className = "flex justify-center items-center p-8";
        wrapper.appendChild(UI.progressCircular(size, true));
        return wrapper;
    },

    // ==================== Tooltip ====================
    
    /**
     * Initialize tooltips for elements with data-tooltip attribute
     */
    initTooltips: () => {
        const container = document.getElementById("tooltip-container") || document.body;
        let currentTooltip = null;
        
        const showTooltip = (element) => {
            const text = element.getAttribute("data-tooltip");
            if (!text) return;
            
            if (currentTooltip) {
                currentTooltip.remove();
            }
            
            const tooltip = document.createElement("div");
            tooltip.className = "md-tooltip";
            tooltip.textContent = text;
            container.appendChild(tooltip);
            
            const rect = element.getBoundingClientRect();
            const tooltipRect = tooltip.getBoundingClientRect();
            
            let top = rect.bottom + 8;
            let left = rect.left + (rect.width - tooltipRect.width) / 2;
            
            if (left < 8) left = 8;
            if (left + tooltipRect.width > window.innerWidth - 8) {
                left = window.innerWidth - tooltipRect.width - 8;
            }
            if (top + tooltipRect.height > window.innerHeight - 8) {
                top = rect.top - tooltipRect.height - 8;
            }
            
            tooltip.style.top = `${top}px`;
            tooltip.style.left = `${left}px`;
            
            requestAnimationFrame(() => {
                tooltip.classList.add("show");
            });
            
            currentTooltip = tooltip;
        };
        
        const hideTooltip = () => {
            if (currentTooltip) {
                currentTooltip.classList.remove("show");
                setTimeout(() => {
                    if (currentTooltip) {
                        currentTooltip.remove();
                        currentTooltip = null;
                    }
                }, 150);
            }
        };
        
        document.addEventListener("mouseenter", (e) => {
            const target = e.target.closest("[data-tooltip]");
            if (target) showTooltip(target);
        }, true);
        
        document.addEventListener("mouseleave", (e) => {
            const target = e.target.closest("[data-tooltip]");
            if (target) hideTooltip();
        }, true);
        
        document.addEventListener("focus", (e) => {
            const target = e.target.closest("[data-tooltip]");
            if (target) showTooltip(target);
        }, true);
        
        document.addEventListener("blur", (e) => {
            const target = e.target.closest("[data-tooltip]");
            if (target) hideTooltip();
        }, true);
    },

    // ==================== Menu Component ====================
    
    /**
     * MD3 Menu Component
     */
    menu: (anchor, items, options = {}) => {
        const { position = "bottom-start" } = options;
        let menuEl = null;
        let isOpen = false;
        
        const close = () => {
            if (menuEl) {
                menuEl.classList.remove("opacity-100", "scale-100");
                menuEl.classList.add("opacity-0", "scale-95");
                setTimeout(() => {
                    menuEl.remove();
                    menuEl = null;
                }, 150);
            }
            isOpen = false;
            document.removeEventListener("click", handleOutsideClick);
            document.removeEventListener("keydown", handleKeydown);
        };
        
        const handleOutsideClick = (e) => {
            if (menuEl && !menuEl.contains(e.target) && !anchor.contains(e.target)) {
                close();
            }
        };
        
        const handleKeydown = (e) => {
            if (e.key === "Escape") {
                close();
            }
        };
        
        const open = () => {
            if (isOpen) {
                close();
                return;
            }
            
            menuEl = document.createElement("div");
            menuEl.className = "fixed bg-md-surface-container rounded-md-xs py-2 min-w-[200px] max-w-[280px] md-elevation-2 z-50 opacity-0 scale-95 transition-all duration-150 origin-top-left";
            
            items.forEach((item) => {
                if (item.divider) {
                    menuEl.appendChild(UI.divider("my-2"));
                    return;
                }
                
                const menuItem = document.createElement("button");
                menuItem.className = `w-full flex items-center gap-4 px-4 py-3 text-body-large text-md-on-surface hover:bg-md-on-surface/8 transition-colors text-left ${item.disabled ? "opacity-38 cursor-not-allowed" : ""}`;
                menuItem.disabled = item.disabled;
                
                if (item.icon) {
                    menuItem.appendChild(UI.icon(item.icon, "text-md-on-surface-variant"));
                }
                
                menuItem.appendChild(UI.el("span", "flex-1", item.label));
                
                if (item.trailing) {
                    menuItem.appendChild(UI.el("span", "text-body-medium text-md-on-surface-variant", item.trailing));
                }
                
                menuItem.onclick = () => {
                    if (!item.disabled && item.onClick) {
                        item.onClick();
                        close();
                    }
                };
                
                menuEl.appendChild(menuItem);
            });
            
            document.body.appendChild(menuEl);
            
            const anchorRect = anchor.getBoundingClientRect();
            const menuRect = menuEl.getBoundingClientRect();
            
            let top, left;
            
            switch (position) {
                case "bottom-end":
                    top = anchorRect.bottom + 4;
                    left = anchorRect.right - menuRect.width;
                    menuEl.style.transformOrigin = "top right";
                    break;
                case "top-start":
                    top = anchorRect.top - menuRect.height - 4;
                    left = anchorRect.left;
                    menuEl.style.transformOrigin = "bottom left";
                    break;
                case "top-end":
                    top = anchorRect.top - menuRect.height - 4;
                    left = anchorRect.right - menuRect.width;
                    menuEl.style.transformOrigin = "bottom right";
                    break;
                default:
                    top = anchorRect.bottom + 4;
                    left = anchorRect.left;
                    menuEl.style.transformOrigin = "top left";
            }
            
            if (left < 8) left = 8;
            if (left + menuRect.width > window.innerWidth - 8) {
                left = window.innerWidth - menuRect.width - 8;
            }
            if (top + menuRect.height > window.innerHeight - 8) {
                top = anchorRect.top - menuRect.height - 4;
            }
            if (top < 8) top = 8;
            
            menuEl.style.top = `${top}px`;
            menuEl.style.left = `${left}px`;
            
            requestAnimationFrame(() => {
                menuEl.classList.remove("opacity-0", "scale-95");
                menuEl.classList.add("opacity-100", "scale-100");
            });
            
            isOpen = true;
            
            setTimeout(() => {
                document.addEventListener("click", handleOutsideClick);
                document.addEventListener("keydown", handleKeydown);
            }, 0);
        };
        
        anchor.addEventListener("click", open);
        
        return { open, close };
    },

    // ==================== Badge Component ====================
    
    /**
     * MD3 Badge Component
     */
    badge: (content = "", variant = "large") => {
        const badge = document.createElement("span");
        
        if (variant === "small" || !content) {
            badge.className = "w-2 h-2 bg-md-error rounded-full";
        } else {
            badge.className = "min-w-[16px] h-4 px-1 bg-md-error text-md-on-error text-label-small font-medium rounded-full flex items-center justify-center";
            badge.textContent = String(content).length > 3 ? "999+" : content;
        }
        
        return badge;
    },

    /**
     * Wrap element with badge
     */
    withBadge: (element, content = "", options = {}) => {
        const { position = "top-right", variant = "large" } = options;
        
        const wrapper = document.createElement("div");
        wrapper.className = "relative inline-flex";
        
        wrapper.appendChild(element);
        
        const badge = UI.badge(content, variant);
        badge.classList.add("absolute");
        
        switch (position) {
            case "top-left":
                badge.classList.add("-top-1", "-left-1");
                break;
            case "bottom-right":
                badge.classList.add("-bottom-1", "-right-1");
                break;
            case "bottom-left":
                badge.classList.add("-bottom-1", "-left-1");
                break;
            default:
                badge.classList.add("-top-1", "-right-1");
        }
        
        wrapper.appendChild(badge);
        
        wrapper.setBadge = (newContent) => {
            if (variant === "large" && newContent) {
                badge.textContent = String(newContent).length > 3 ? "999+" : newContent;
                badge.style.display = "";
            } else if (!newContent && variant === "large") {
                badge.style.display = "none";
            }
        };
        
        wrapper.hideBadge = () => {
            badge.style.display = "none";
        };
        
        wrapper.showBadge = () => {
            badge.style.display = "";
        };
        
        return wrapper;
    },

    // ==================== Snackbar with Queue ====================
    
    _snackbarQueue: [],
    _currentSnackbar: null,
    
    /**
     * MD3 Snackbar with queue management
     */
    snackbar: (message, actionText = null, onAction = null, options = {}) => {
        const { duration = 4000, variant = "default" } = options;
        
        const showSnackbar = () => {
            const container = document.getElementById("snackbar-container") || document.body;
            
            const snackbar = document.createElement("div");
            
            // 根据 variant 设置不同的背景和文字颜色
            const variantStyles = {
                default: "bg-gray-800 text-white",
                error: "bg-red-700 text-white",
                success: "bg-green-700 text-white",
                info: "bg-blue-700 text-white",
                warning: "bg-amber-600 text-white"
            };
            
            const styleClass = variantStyles[variant] || variantStyles.default;
            snackbar.className = `${styleClass} px-4 py-3 rounded-md-xs md-elevation-3 flex items-center gap-4 min-w-[344px] max-w-[672px] animate-slide-up pointer-events-auto`;
            
            const messageEl = UI.el("span", "flex-1 text-body-medium", message);
            snackbar.appendChild(messageEl);
            
            if (actionText) {
                const actionBtn = document.createElement("button");
                actionBtn.className = `font-medium text-label-large px-2 py-1 rounded hover:bg-white/8 transition-colors ${
                    variant === "default" ? "text-md-inverse-primary" : ""
                }`;
                actionBtn.textContent = actionText;
                actionBtn.onclick = () => {
                    if (onAction) onAction();
                    dismissSnackbar();
                };
                snackbar.appendChild(actionBtn);
            }
            
            container.appendChild(snackbar);
            UI._currentSnackbar = snackbar;
            
            const dismissSnackbar = () => {
                snackbar.classList.add("opacity-0", "translate-y-2", "transition-all", "duration-200");
                setTimeout(() => {
                    snackbar.remove();
                    UI._currentSnackbar = null;
                    
                    if (UI._snackbarQueue.length > 0) {
                        const next = UI._snackbarQueue.shift();
                        next();
                    }
                }, 200);
            };
            
            if (duration > 0) {
                setTimeout(dismissSnackbar, duration);
            }
            
            return dismissSnackbar;
        };
        
        if (UI._currentSnackbar) {
            UI._snackbarQueue.push(showSnackbar);
        } else {
            showSnackbar();
        }
    },

    // ==================== Dialog/Modal ====================
    
    /**
     * MD3 Dialog/Modal Component
     */
    dialog: (title, renderContentFn, onSave, saveText = "保存", options = {}) => {
        const { cancelText = "取消", showCancel = true, width = "max-w-lg" } = options;
        
        const scrim = document.createElement("div");
        scrim.className = "fixed inset-0 bg-black/32 z-50 flex items-center justify-center p-4 animate-fade-in";
        
        const dialog = document.createElement("div");
        dialog.className = `bg-md-surface rounded-md-xl md-elevation-3 w-full ${width} max-h-[90vh] flex flex-col animate-scale-in`;
        
        const header = document.createElement("div");
        header.className = "flex items-center justify-between p-6 border-b border-md-outline-variant";
        
        const titleEl = UI.el("h2", "text-headline-small text-md-on-surface", title);
        const closeBtn = UI.iconBtn("close", () => scrim.remove());
        
        header.appendChild(titleEl);
        header.appendChild(closeBtn);
        
        const body = document.createElement("div");
        body.className = "flex-1 overflow-y-auto p-6";
        const content = renderContentFn();
        if (content) body.appendChild(content);
        
        const footer = document.createElement("div");
        footer.className = "flex justify-end gap-3 p-6 border-t border-md-outline-variant";
        
        if (showCancel) {
            const cancelBtn = UI.btn(cancelText, () => scrim.remove(), "text");
            footer.appendChild(cancelBtn);
        }
        
        const saveBtn = UI.btn(
            saveText,
            async () => {
                if (!onSave) {
                    scrim.remove();
                    return;
                }
                saveBtn.setLoading(true);
                try {
                    const result = await onSave();
                    if (result !== false) scrim.remove();
                } finally {
                    saveBtn.setLoading(false);
                }
            },
            "filled"
        );
        
        footer.appendChild(saveBtn);
        
        dialog.appendChild(header);
        dialog.appendChild(body);
        dialog.appendChild(footer);
        scrim.appendChild(dialog);
        
        // 防止拖动选择文字时意外关闭：只有在 mousedown 和 mouseup 都发生在 scrim 上才关闭
        let mouseDownOnScrim = false;
        scrim.addEventListener("mousedown", (e) => {
            mouseDownOnScrim = (e.target === scrim);
        });
        scrim.addEventListener("mouseup", (e) => {
            if (mouseDownOnScrim && e.target === scrim) {
                scrim.remove();
            }
            mouseDownOnScrim = false;
        });
        
        const handleEscape = (e) => {
            if (e.key === "Escape") {
                scrim.remove();
                document.removeEventListener("keydown", handleEscape);
            }
        };
        document.addEventListener("keydown", handleEscape);
        
        document.body.appendChild(scrim);
        
        return { close: () => scrim.remove() };
    },

    /**
     * MD3 Side Sheet (Right Drawer) Component
     */
    sideSheet: (title, renderContentFn, onSave, saveText = "保存", options = {}) => {
        const { width = "max-w-3xl", cancelText = "取消" } = options;
        
        const scrim = document.createElement("div");
        scrim.className = "fixed inset-0 bg-black/32 backdrop-blur-sm z-50 flex justify-end opacity-0 transition-opacity duration-200";
        
        const sheet = document.createElement("div");
        sheet.className = `w-full ${width} h-full bg-md-surface md-elevation-3 border-l border-md-outline-variant flex flex-col transform translate-x-full transition-transform duration-200 ease-out`;
        
        const header = document.createElement("div");
        header.className = "flex items-center justify-between px-6 py-4 border-b border-md-outline-variant";
        
        const titleEl = UI.el("h2", "text-headline-small text-md-on-surface", title);
        
        const closeWithAnimation = () => {
            sheet.classList.remove("translate-x-0");
            sheet.classList.add("translate-x-full");
            scrim.classList.remove("opacity-100");
            scrim.classList.add("opacity-0");
            setTimeout(() => {
                scrim.remove();
            }, 200);
        };
        
        const closeBtn = UI.iconBtn("close", closeWithAnimation);
        
        header.appendChild(titleEl);
        header.appendChild(closeBtn);
        
        const body = document.createElement("div");
        body.className = "flex-1 overflow-y-auto p-6";
        const content = renderContentFn();
        if (content) body.appendChild(content);
        
        const footer = document.createElement("div");
        footer.className = "flex justify-end gap-3 px-6 py-4 border-t border-md-outline-variant";
        
        const cancelBtn = UI.btn(cancelText, closeWithAnimation, "text");
        const saveBtn = UI.btn(
            saveText,
            async () => {
                if (!onSave) {
                    closeWithAnimation();
                    return;
                }
                saveBtn.setLoading(true);
                try {
                    const result = await onSave();
                    if (result !== false) {
                        closeWithAnimation();
                    }
                } finally {
                    saveBtn.setLoading(false);
                }
            },
            "filled"
        );
        
        footer.appendChild(cancelBtn);
        footer.appendChild(saveBtn);
        
        sheet.appendChild(header);
        sheet.appendChild(body);
        sheet.appendChild(footer);
        scrim.appendChild(sheet);
        
        // 防止拖动选择文字时意外关闭：只有在 mousedown 和 mouseup 都发生在 scrim 上才关闭
        let mouseDownOnScrim = false;
        scrim.addEventListener("mousedown", (e) => {
            mouseDownOnScrim = (e.target === scrim);
        });
        scrim.addEventListener("mouseup", (e) => {
            if (mouseDownOnScrim && e.target === scrim) {
                closeWithAnimation();
            }
            mouseDownOnScrim = false;
        });
        
        const handleEscape = (e) => {
            if (e.key === "Escape") {
                closeWithAnimation();
                document.removeEventListener("keydown", handleEscape);
            }
        };
        document.addEventListener("keydown", handleEscape);
        
        document.body.appendChild(scrim);
        
        requestAnimationFrame(() => {
            sheet.classList.remove("translate-x-full");
            sheet.classList.add("translate-x-0");
            scrim.classList.remove("opacity-0");
            scrim.classList.add("opacity-100");
        });
        
        return { close: closeWithAnimation };
    },

    // ==================== Divider ====================
    
    /**
     * MD3 Divider
     */
    divider: (classes = "") => {
        const div = document.createElement("div");
        div.className = `h-px bg-md-outline-variant ${classes}`;
        return div;
    },

    // ==================== List Item ====================
    
    /**
     * MD3 List Item
     */
    listItem: (title, subtitle = null, iconName = null, onClick = null, options = {}) => {
        const { trailing = null, disabled = false } = options;
        
        const item = document.createElement("div");
        item.className = `flex items-center gap-4 px-4 py-3 hover:bg-md-on-surface/8 rounded-md-xs cursor-pointer md-state-layer transition-colors ${disabled ? "opacity-38 pointer-events-none" : ""}`;
        
        if (iconName) {
            const iconContainer = document.createElement("div");
            iconContainer.className = "w-10 h-10 rounded-full bg-md-primary-container flex items-center justify-center text-md-on-primary-container";
            iconContainer.appendChild(UI.icon(iconName));
            item.appendChild(iconContainer);
        }
        
        const textContainer = document.createElement("div");
        textContainer.className = "flex-1 min-w-0";
        
        const titleEl = UI.el("div", "text-body-large text-md-on-surface truncate", title);
        textContainer.appendChild(titleEl);
        
        if (subtitle) {
            const subtitleEl = UI.el("div", "text-body-medium text-md-on-surface-variant mt-0.5 truncate", subtitle);
            textContainer.appendChild(subtitleEl);
        }
        
        item.appendChild(textContainer);
        
        if (trailing) {
            if (typeof trailing === "string") {
                item.appendChild(UI.el("span", "text-body-medium text-md-on-surface-variant", trailing));
            } else {
                item.appendChild(trailing);
            }
        }
        
        if (onClick) item.onclick = onClick;
        
        return item;
    },

    // ==================== Select Component ====================
    
    /**
     * MD3 Select (Dropdown)
     */
    select: (label, options, value = "", onChange = null, selectOptions = {}) => {
        const { disabled = false, required = false, error = false, helperText = "" } = selectOptions;
        
        const wrapper = document.createElement("div");
        wrapper.className = "relative mb-4";
        
        const select = document.createElement("select");
        select.className = `peer w-full px-4 pt-6 pb-2 bg-transparent border rounded-md-xs text-body-large text-md-on-surface focus:outline-none focus:border-2 transition-all appearance-none cursor-pointer disabled:opacity-38 disabled:cursor-not-allowed ${
            error 
                ? "border-md-error focus:border-md-error" 
                : "border-md-outline focus:border-md-primary"
        }`;
        select.disabled = disabled;
        select.required = required;
        
        options.forEach((opt) => {
            const optEl = document.createElement("option");
            optEl.value = opt.value;
            optEl.textContent = opt.label;
            if (opt.value === value) optEl.selected = true;
            select.appendChild(optEl);
        });
        
        const labelEl = UI.el("label", `absolute left-4 top-2 text-body-small font-medium pointer-events-none transition-all duration-200 ${error ? "text-md-error" : "text-md-on-surface-variant peer-focus:text-md-primary"}`, label + (required ? " *" : ""));
        
        const arrow = UI.icon("arrow_drop_down", "absolute right-3 top-1/2 -translate-y-1/2 text-md-on-surface-variant pointer-events-none");
        
        wrapper.appendChild(select);
        wrapper.appendChild(labelEl);
        wrapper.appendChild(arrow);
        
        if (helperText) {
            const helperEl = UI.el("div", `text-body-small mt-1 px-4 ${error ? "text-md-error" : "text-md-on-surface-variant"}`, helperText);
            wrapper.appendChild(helperEl);
        }
        
        if (onChange) {
            select.onchange = (e) => onChange(e.target.value);
        }
        
        return { wrapper, select };
    }
};

// Initialize tooltips when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
    UI.initTooltips();
});
