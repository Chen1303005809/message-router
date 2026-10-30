import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './style.css'

const privateIntakeRoute = /\/issues\/private-intake(?:\/continue)?\/?$/
if (privateIntakeRoute.test(window.location.pathname) && window.location.hash.length > 1) {
  sessionStorage.setItem('kefu_private_intake_token', window.location.hash.slice(1))
  history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
}

createApp(App).use(createPinia()).use(router).mount('#app')
