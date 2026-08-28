# cBioPortal Database Exporter

## Frontend development

```shell
nvm use
npm ci
npm run dev
```

The development server renders current public dump metadata from the CDN
configured by `VITE_CDN_ORIGIN` in `.env.development`.

Create a static production build:

```shell
npm run build
```

The generated files are written to `dist/`.
