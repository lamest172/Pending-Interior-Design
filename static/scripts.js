document.addEventListener('DOMContentLoaded', function() {
    const viewershipInput = document.getElementById('viewership_weight');
    const shortlistInput = document.getElementById('shortlist_weight');
    const form = document.querySelector('form');

    if (viewershipInput && shortlistInput) {
        // When viewership changes, auto-update shortlist
        viewershipInput.addEventListener('input', function() {
            let val = parseInt(viewershipInput.value);
            if (isNaN(val)) return;
            
            // Clamp value between 0 and 100
            val = Math.max(0, Math.min(100, val));
            
            // Auto-calculate the other field
            shortlistInput.value = 100 - val;
        });

        // When shortlist changes, auto-update viewership
        shortlistInput.addEventListener('input', function() {
            let val = parseInt(shortlistInput.value);
            if (isNaN(val)) return;
            
            // Clamp value between 0 and 100
            val = Math.max(0, Math.min(100, val));
            
            // Auto-calculate the other field
            viewershipInput.value = 100 - val;
        });
    }

    // Fallback form submission check
    if (form) {
        form.addEventListener('submit', function(e) {
            const viewership = parseInt(viewershipInput.value) || 0;
            const shortlist = parseInt(shortlistInput.value) || 0;
            
            if (viewership + shortlist !== 100) {
                e.preventDefault();
                alert('The total weightages must equal 100%.');
            }
        });
    }
});

function openAllMetricsModal() {
    document.getElementById('allMetricsModal').classList.add('active');
}
function closeAllMetricsModal() {
    document.getElementById('allMetricsModal').classList.remove('active');
}
function handleOutsideClick(event, id) {
    if (event.target.id === id) {
        closeAllMetricsModal();
    }
}