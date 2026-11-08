import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import StaffApp from './staff/StaffApp'
import './staff/staff.css'

// Two apps, one bundle: patients at /, clinic staff at /staff
const Root = window.location.pathname.startsWith('/staff') ? StaffApp : App

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
