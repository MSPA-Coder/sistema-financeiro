(function () {
    function normalize(value) {
        return (value || '').toString().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    }

    document.querySelectorAll('[data-filter-target]').forEach(function (input) {
        input.addEventListener('input', function () {
            var target = input.getAttribute('data-filter-target');
            var term = normalize(input.value);
            document.querySelectorAll('[data-filter-row="' + target + '"]').forEach(function (row) {
                row.style.display = normalize(row.textContent).includes(term) ? '' : 'none';
            });
        });
    });

    document.querySelectorAll('[data-owner-bulk]').forEach(function (button) {
        button.addEventListener('click', function () {
            var action = button.getAttribute('data-owner-bulk');
            document.querySelectorAll('[data-filter-row="owner-row"]').forEach(function (row) {
                if (row.style.display === 'none') { return; }
                var checkbox = row.querySelector('[data-owner-action="' + action + '"]');
                var view = row.querySelector('[data-owner-action="view"]');
                if (checkbox) { checkbox.checked = true; }
                if (view && action !== 'view') { view.checked = true; }
            });
        });
    });

    var ownerClear = document.querySelector('[data-owner-clear]');
    if (ownerClear) {
        ownerClear.addEventListener('click', function () {
            document.querySelectorAll('[data-filter-row="owner-row"]').forEach(function (row) {
                if (row.style.display === 'none') { return; }
                row.querySelectorAll('input[type="checkbox"]').forEach(function (checkbox) { checkbox.checked = false; });
            });
        });
    }

    var permissionBulk = document.querySelector('[data-permission-bulk]');
    function permissionCheckboxForKey(permissionKey) {
        var match = null;
        document.querySelectorAll('[data-permission-key]').forEach(function (checkbox) {
            if (checkbox.getAttribute('data-permission-key') === permissionKey) {
                match = checkbox;
            }
        });
        return match;
    }

    function applyPermissionDependencies(checkbox) {
        if (!checkbox || !checkbox.checked) { return; }
        var implied = (checkbox.getAttribute('data-implies-permissions') || '').split(',');
        implied.forEach(function (permissionKey) {
            permissionKey = permissionKey.trim();
            if (!permissionKey) { return; }
            var impliedCheckbox = permissionCheckboxForKey(permissionKey);
            if (!impliedCheckbox || impliedCheckbox.checked) { return; }
            impliedCheckbox.checked = true;
            applyPermissionDependencies(impliedCheckbox);
        });
    }

    document.querySelectorAll('[data-permission-checkbox]').forEach(function (checkbox) {
        checkbox.addEventListener('change', function () {
            applyPermissionDependencies(checkbox);
        });
        applyPermissionDependencies(checkbox);
    });

    if (permissionBulk) {
        permissionBulk.addEventListener('click', function () {
            document.querySelectorAll('[data-filter-row="permission-row"]').forEach(function (row) {
                if (row.style.display === 'none') { return; }
                var checkbox = row.querySelector('[data-permission-checkbox]');
                if (checkbox) {
                    checkbox.checked = true;
                    applyPermissionDependencies(checkbox);
                }
            });
        });
    }

    var permissionClear = document.querySelector('[data-permission-clear]');
    if (permissionClear) {
        permissionClear.addEventListener('click', function () {
            document.querySelectorAll('[data-filter-row="permission-row"]').forEach(function (row) {
                if (row.style.display === 'none') { return; }
                var checkbox = row.querySelector('[data-permission-checkbox]');
                if (checkbox && !checkbox.disabled) { checkbox.checked = false; }
            });
        });
    }
})();
