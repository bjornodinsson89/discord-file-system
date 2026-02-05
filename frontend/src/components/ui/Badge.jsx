/**
 * Badge Component
 * Status badges for sessions, raffles, etc.
 */

import React from 'react';

const VARIANTS = {
  default: 'bg-gray-600 text-gray-200',
  success: 'bg-green-600 text-green-100',
  warning: 'bg-yellow-600 text-yellow-100',
  error: 'bg-red-600 text-red-100',
  info: 'bg-blue-600 text-blue-100',
  purple: 'bg-purple-600 text-purple-100',
  pink: 'bg-pink-600 text-pink-100',
  teal: 'bg-teal-600 text-teal-100',
};

const STATUS_VARIANTS = {
  // Sessions
  open: 'success',
  locked: 'warning',
  completed: 'info',
  cancelled: 'error',

  // Raffles
  active: 'success',
  drawing: 'warning',

  // Claims & Providers (same keys)
  pending: 'warning',
  approved: 'success',
  rejected: 'error',
  paid: 'info',
  disabled: 'error',
};

export default function Badge({ 
  children, 
  variant = 'default',
  status,
  size = 'sm',
  className = '' 
}) {
  // Use status to determine variant if provided
  const resolvedVariant = status ? (STATUS_VARIANTS[status] || 'default') : variant;
  const variantClasses = VARIANTS[resolvedVariant] || VARIANTS.default;
  
  const sizeClasses = size === 'sm' 
    ? 'px-2 py-0.5 text-xs' 
    : 'px-3 py-1 text-sm';

  return (
    <span 
      className={`
        inline-flex items-center font-medium rounded-full
        ${variantClasses}
        ${sizeClasses}
        ${className}
      `}
    >
      {children}
    </span>
  );
}
