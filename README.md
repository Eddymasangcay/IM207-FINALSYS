# Chukakoy Car Rental System

<div align="center">

[![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Railway](https://img.shields.io/badge/Railway-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app)
[![PayMongo](https://img.shields.io/badge/PayMongo-6A1B9A?style=for-the-badge&logo=moneygram&logoColor=white)](https://paymongo.com)
[![Figma](https://img.shields.io/badge/Figma-F24E1E?style=for-the-badge&logo=figma&logoColor=white)](https://www.figma.com/board/t3X2NkmHzongN0VsYPoC4D/Chukakoy-Car-Rental---Entity-Relationship-Diagram?node-id=0-1&t=pq38QzFM8nLPN6Wv-1)

</div>

---

## 📌 Overview

**Chukakoy Car Rental System** is a web-based academic project developed to streamline and modernize day-to-day car rental operations.  
The system is designed to provide a smoother experience for both customers and administrators by combining booking management, vehicle tracking, and payment processing in one platform.

For customers, the platform offers an accessible way to browse available vehicles, select preferred rental schedules, and complete reservations online.  
For administrators, it provides tools to manage car inventory, monitor booking activity, verify payment status, and keep rental records organized and up to date.

The project also integrates secure online payment support through PayMongo and is deployed using Railway for cloud accessibility.  
Overall, Chukakoy Car Rental System serves as a practical and user-focused solution that improves efficiency, reduces manual processes, and supports more reliable rental service management.

---

## ✨ Features

- 🚗 Vehicle listing and availability management  
- 📅 Online reservation and scheduling  
- 💳 Payment integration (PayMongo)  
- 👤 User authentication and account management  
- 🛠️ Admin dashboard for fleet and booking control  
- ☁️ Cloud deployment via Railway  

---

## 🧰 Tech Stack

- **Backend:** Python, Flask  
- **Frontend:** HTML, CSS, JavaScript  
- **Database:** PostgreSQL  
- **Deployment:** Railway  
- **Payments:** PayMongo  
- **Design/ERD:** Figma  

---

## 🗂️ Entity-Relationship Diagram (ERD)

<div align="center">

### Database Structure Overview  
Design and relationship mapping for the **Chukakoy Car Rental System**

🔗 **[Open ERD in Figma](https://www.figma.com/board/t3X2NkmHzongN0VsYPoC4D/Chukakoy-Car-Rental---Entity-Relationship-Diagram?node-id=0-1&t=pq38QzFM8nLPN6Wv-1)**

</div>

---

## 🧪 How to Run Locally

### 1) Clone the repository

    git clone https://github.com/your-username/your-repo-name.git
    cd your-repo-name

### 2) Create and activate a virtual environment

**Windows (PowerShell)**

    python -m venv venv
    venv\Scripts\Activate

**macOS/Linux**

    python3 -m venv venv
    source venv/bin/activate

### 3) Install dependencies

    pip install -r requirements.txt

### 4) Configure environment variables

Create a `.env` file in the project root, then add:

    DATABASE_URL=postgresql://user:password@host:5432/dbname
    PAYMONGO_SECRET_KEY=sk_test_or_live_replace_me
    PAYMONGO_WEBHOOK_SECRET=whsk_replace_me
    PUBLIC_APP_URL=http://127.0.0.1:5000
    FLASK_SECRET_KEY=replace_with_a_long_random_string
    PAYMONGO_PAYMENT_METHOD_TYPES=card,gcash,paymaya,grab_pay,shopee_pay
    USD_TO_PHP_RATE=56.0

### 5) Run the application

    python main.py

### 6) Open in browser

`http://127.0.0.1:5000`

---

## 🌐 Live Demo

Experience the deployed system here:  
🔗 [Chukakoy Car Rental System (Railway)](https://chukakoycars.up.railway.app/)

---

## 👨‍💻 Developers

<div align="center">

<table>
  <tr>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" />
      <br />
      <strong>Eduardo D. Masangcay (Me)</strong>
      <br />
      <em>Web Developer</em>
    </td>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" />
      <br />
      <strong>John Paul G. Natad</strong>
      <br />
      <em>Database Analyst</em>
    </td>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" />
      <br />
      <strong>Jerkean C. Gabrina</strong>
      <br />
      <em>Assistant Database Analyst</em>
    </td>
  </tr>
  <tr>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" />
      <br />
      <strong>John Peter B. Gale</strong>
      <br />
      <em>Documentator</em>
    </td>
    <td align="center">
      <img src="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png" width="90" height="90" />
      <br />
      <strong>Vince Gabriel Paquiabas</strong>
      <br />
      <em>Designer</em>
    </td>
  </tr>
</table>

</div>

---

## 🚀 Deployment

This project is deployed using **Railway** for easy cloud hosting and continuous deployment.

---

## 🙏 Acknowledgements

Special thanks to the following platforms and tools that made this project possible:

- [Flask](https://flask.palletsprojects.com/) — backend web framework  
- [PostgreSQL](https://www.postgresql.org/) — relational database management  
- [Railway](https://railway.app/) — deployment and cloud hosting  
- [PayMongo](https://paymongo.com/) — online payment gateway integration  
- [Figma](https://www.figma.com/) — ERD and design planning  
- [Shields.io](https://shields.io/) — technology badges used in this README  

---

## 📜 License

This project is developed for academic purposes at **St. Cecilia's College-Cebu, Inc.**

© 2026 Eduardo D. Masangcay — BSIT 2-E
