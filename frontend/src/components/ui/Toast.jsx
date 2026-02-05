/**
 * Toast - Global toast notification system
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import * as Toast from '@radix-ui/react-toast';

const ToastContext = createContext(null);

const VARIANT_STYLES = {
  success: 'border-green-500/40 bg-green-500/10 text-green-100',
  error: 'border-red-500/40 bg-red-500/10 text-red-100',
  warning: 'border-yellow-500/40 bg-yellow-500/10 text-yellow-100',
  info: 'border-blue-500/40 bg-blue-500/10 text-blue-100',
};

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((toast) => toast.id !== id));
  }, []);

  const addToast = useCallback(({ title, description, variant = 'info' }) => {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setToasts((prev) => [
      ...prev,
      { id, title, description, variant },
    ]);
    return id;
  }, []);

  const value = useMemo(() => ({ addToast }), [addToast]);

  return (
    <ToastContext.Provider value={value}>
      <Toast.Provider swipeDirection="right">
        {children}
        {toasts.map((toast) => (
          <Toast.Root
            key={toast.id}
            open
            duration={5000}
            onOpenChange={(open) => {
              if (!open) dismissToast(toast.id);
            }}
            className={`border rounded-xl shadow-lg px-4 py-3 grid gap-1 w-[320px] ${VARIANT_STYLES[toast.variant] || VARIANT_STYLES.info}`}
          >
            {toast.title && (
              <Toast.Title className="text-sm font-semibold text-white">
                {toast.title}
              </Toast.Title>
            )}
            {toast.description && (
              <Toast.Description className="text-xs text-gray-200">
                {toast.description}
              </Toast.Description>
            )}
          </Toast.Root>
        ))}
        <Toast.Viewport className="fixed bottom-4 right-4 z-50 flex flex-col gap-3 outline-none" />
      </Toast.Provider>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
}
