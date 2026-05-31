FROM node:20-alpine
WORKDIR /app
COPY dashboard/package.json .
RUN npm install --production
COPY dashboard/ .
EXPOSE 3000
CMD ["node", "server.js"]
