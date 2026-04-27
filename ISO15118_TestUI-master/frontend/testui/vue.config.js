module.exports = {
  transpileDependencies: [
    'vuetify',
    '@koumoul/vjsf'
  ],
  lintOnSave: false,
  devServer: {
    port: 8080,
    host: '0.0.0.0',
    open: false,
    disableHostCheck: true
  },
  css: {
    loaderOptions: {
      sass: {
        sassOptions: {
          outputStyle: 'expanded'
        }
      }
    }
  }
}
