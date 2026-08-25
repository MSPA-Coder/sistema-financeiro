(function () {
    "use strict";

    var form = document.getElementById("theme-form");
    if (!form) return;

    var selected = form.querySelector('input[name="theme"]:checked');
    form.querySelectorAll('input[name="theme"]').forEach(function (input) {
        input.addEventListener("change", function () {
            var previous = selected;
            if (!window.sharedauth || typeof window.sharedauth.confirmar !== "function") {
                input.checked = false;
                if (previous) previous.checked = true;
                return;
            }
            window.sharedauth.confirmar({
                mensagem: "Salvar este tema como sua preferência?",
                titulo: "Confirmar tema",
                severidade: "info"
            }).then(function (ok) {
                if (!ok) {
                    input.checked = false;
                    if (previous) previous.checked = true;
                    return;
                }
                selected = input;
                form.requestSubmit();
            });
        });
    });
})();
