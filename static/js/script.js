// VyahMandap JavaScript Utilities

// Format currency
function formatCurrency(amount) {
    return '₹' + Number(amount).toFixed(2);
}

// Get status badge color
function getStatusColor(status) {
    const colors = {
        'confirmed': 'bg-green-100 text-green-800',
        'pending': 'bg-yellow-100 text-yellow-800',
        'cancelled': 'bg-red-100 text-red-800',
        'completed': 'bg-blue-100 text-blue-800'
    };
    return colors[status] || 'bg-gray-100 text-gray-800';
}

// Show toast notification
function showToast(message, type = 'success') {
    const colors = {
        success: 'bg-green-500',
        error: 'bg-red-500',
        warning: 'bg-yellow-500',
        info: 'bg-blue-500'
    };
    
    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 ${colors[type] || 'bg-gray-800'} text-white px-6 py-3 rounded-lg shadow-lg text-sm z-50 max-w-sm transition-opacity duration-300`;
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Handle image errors
function handleImageError(img) {
    img.src = '/static/images/default-item.jpg';
    img.onerror = null;
}

// Auto dismiss flash messages
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.flash-message').forEach(msg => {
        setTimeout(() => {
            msg.style.transition = 'opacity 0.5s';
            msg.style.opacity = '0';
            setTimeout(() => msg.remove(), 500);
        }, 4000);
    });
});

// Confirm before delete
function confirmDelete(message = 'Are you sure you want to delete this?') {
    return confirm(message);
}

// Mobile menu toggle
function toggleMobileMenu() {
    const menu = document.getElementById('mobileMenu');
    if (menu) {
        menu.classList.toggle('hidden');
    }
}

// Smooth scroll to element
function scrollToElement(elementId) {
    const el = document.getElementById(elementId);
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// Format date for display
function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric'
    });
}