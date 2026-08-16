(function () {
    "use strict";

    var form = document.getElementById("theme-form");
    if (!form) return;

    form.querySelectorAll('input[name="theme"]').forEach(function (input) {
        input.addEventListener("change", function () {
            form.requestSubmit();
        });
    });
})();
